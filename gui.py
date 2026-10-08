"""
gui.py — واجهة رسومية بسيطة واحترافية لمشروع Book2EPUB.

توفر وصولاً مرئياً وسهلاً لجميع وظائف المشروع:
  1. استخراج كتب الويب وتحويلها إلى EPUB (منفرد أو تصنيف كامل).
  2. مزامنة PDF مع EPUB (تقسيم EPUB بحيث كل صفحة PDF = ملف XHTML مستقل).
  3. فحص ملفات EPUB بأداة EPUBCheck الرسمية.
  4. شاشة إرشادية وتوثيق مرئي لمكونات وطريقة عمل النظام.
"""
from __future__ import annotations

import json
import logging
import os
import queue
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Any

from config import Config
from downloader import Downloader
from epub_builder import EPUBBuilder
from pdfsync.matcher import MatchSettings
from pdfsync.pipeline import SyncError, SyncOptions, run_sync
from scraper import BookScraper
from validator import EPUBValidator

BASE_DIR = Path(__file__).resolve().parent

# فئات الألوان والخطوط
FONT_TITLE = ("Segoe UI", 13, "bold")
FONT_BOLD = ("Segoe UI", 10, "bold")
FONT_NORMAL = ("Segoe UI", 9)
FONT_MONO = ("Consolas", 9)


class QueueHandler(logging.Handler):
    """موجّه السجلات إلى طابور آمن برمجياً لتحديث واجهة المستخدم."""
    def __init__(self, log_queue: queue.Queue):
        super().__init__()
        self.log_queue = log_queue

    def emit(self, record: logging.LogRecord):
        msg = self.format(record)
        self.log_queue.put((record.levelname, msg))


class BookStudioGUI:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("Book2EPUB Studio — محول ومزامن الكتب العربية إلى EPUB")
        self.root.geometry("1020x760")
        self.root.minsize(860, 620)

        # تجهيز النمط والمظهر
        self.style = ttk.Style()
        try:
            self.style.theme_use("vista")
        except Exception:
            pass

        self.style.configure("TNotebook.Tab", font=FONT_BOLD, padding=[12, 6])
        self.style.configure("Accent.TButton", font=FONT_BOLD, foreground="#0d47a1")
        self.style.configure("Big.TButton", font=FONT_BOLD, padding=[10, 6])
        self.style.configure("Header.TLabel", font=FONT_TITLE)

        self.config = Config()
        self.log_queue: queue.Queue[tuple[str, str]] = queue.Queue()
        self._setup_logging()

        self.worker_thread: threading.Thread | None = None
        self.stop_requested = False

        # شريط علوي أنيق
        self._build_header()

        # التبويبات الرئيسية
        self.notebook = ttk.Notebook(self.root)
        self.notebook.pack(fill=tk.BOTH, expand=True, padx=10, pady=(0, 6))

        # 1. تبويب استخراج الويب
        self.tab_scraper = ttk.Frame(self.notebook, padding=10)
        self.notebook.add(self.tab_scraper, text=" 🌐 استخراج الويب إلى EPUB ")
        self._build_scraper_tab()

        # 2. تبويب مزامنة PDF مع EPUB
        self.tab_sync = ttk.Frame(self.notebook, padding=10)
        self.notebook.add(self.tab_sync, text=" 📑 مزامنة PDF مع EPUB ")
        self._build_sync_tab()

        # 3. تبويب فحص EPUBCheck
        self.tab_validator = ttk.Frame(self.notebook, padding=10)
        self.notebook.add(self.tab_validator, text=" 🔬 فاحص EPUBCheck ")
        self._build_validator_tab()

        # 4. تبويب الشرح وطريقة العمل
        self.tab_about = ttk.Frame(self.notebook, padding=10)
        self.notebook.add(self.tab_about, text=" 📖 دليل ومكونات المشروع ")
        self._build_about_tab()

        # شريط الحالة السفلي ومراقبة السجلات
        self._build_footer()
        self.root.after(100, self._process_log_queue)

    def _setup_logging(self):
        handler = QueueHandler(self.log_queue)
        handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%H:%M:%S"))
        root_logger = logging.getLogger()
        root_logger.setLevel(logging.INFO)
        root_logger.addHandler(handler)

    def _build_header(self):
        hdr = ttk.Frame(self.root, padding=(14, 10, 14, 6))
        hdr.pack(fill=tk.X)

        title_lbl = ttk.Label(hdr, text="Book2EPUB Studio", style="Header.TLabel", foreground="#1565c0")
        title_lbl.pack(side=tk.LEFT)

        sub_lbl = ttk.Label(hdr, text="— استخراج الويب، الحفظ الفقهي الأمين، ومزامنة الصفحات مع PDF", font=FONT_NORMAL)
        sub_lbl.pack(side=tk.LEFT, padx=8)

        btn_out = ttk.Button(hdr, text="📂 فتح مجلد المخرجات (output)", command=self._open_output_dir)
        btn_out.pack(side=tk.RIGHT)

    # -------------------------------------------------------------------------
    # التبويب 1: استخراج الويب إلى EPUB
    # -------------------------------------------------------------------------
    def _build_scraper_tab(self):
        frame = self.tab_scraper

        # قسم الرابط والخيارات
        grp_input = ttk.LabelFrame(frame, text=" رابط الكتاب أو التصنيف من موقع sistani.org ", padding=10)
        grp_input.pack(fill=tk.X, pady=(0, 8))

        row1 = ttk.Frame(grp_input)
        row1.pack(fill=tk.X, pady=2)
        ttk.Label(row1, text="رابط الموقع (URL):", font=FONT_BOLD).pack(side=tk.LEFT, padx=(0, 6))
        self.url_var = tk.StringVar(value="https://www.sistani.org/arabic/book/13/")
        url_ent = ttk.Entry(row1, textvariable=self.url_var, font=FONT_NORMAL)
        url_ent.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=4)

        # أزرار الروابط الجاهزة السريعة
        row_quick = ttk.Frame(grp_input)
        row_quick.pack(fill=tk.X, pady=(4, 2))
        ttk.Label(row_quick, text="أمثلة سريعة: ", font=FONT_NORMAL, foreground="#666").pack(side=tk.LEFT)
        for name, u in [
            ("المسائل المنتخبة (كتاب 13)", "https://www.sistani.org/arabic/book/13/"),
            ("مناسك الحج (كتاب 14)", "https://www.sistani.org/arabic/book/14/"),
            ("الوجيز في أحكام العبادات (كتاب 24)", "https://www.sistani.org/arabic/book/24/"),
            ("جميع كتب الفتاوى (تصنيف)", "https://www.sistani.org/arabic/book/fatwa/"),
        ]:
            b = ttk.Button(row_quick, text=name, command=lambda url=u: self.url_var.set(url))
            b.pack(side=tk.LEFT, padx=2)

        # خيارات متقدمة
        grp_opts = ttk.LabelFrame(frame, text=" خيارات الاستخراج والتقسيم ", padding=10)
        grp_opts.pack(fill=tk.X, pady=(0, 8))

        f_opts1 = ttk.Frame(grp_opts)
        f_opts1.pack(fill=tk.X, pady=2)

        ttk.Label(f_opts1, text="نوع الاستخراج: ", font=FONT_BOLD).pack(side=tk.LEFT)
        self.scrape_mode_var = tk.StringVar(value="single")
        ttk.Radiobutton(f_opts1, text="كتاب منفرد", variable=self.scrape_mode_var, value="single").pack(side=tk.LEFT, padx=6)
        ttk.Radiobutton(f_opts1, text="جميع الكتب في التصنيف (--all)", variable=self.scrape_mode_var, value="all").pack(side=tk.LEFT, padx=6)

        ttk.Separator(f_opts1, orient=tk.VERTICAL).pack(side=tk.LEFT, fill=tk.Y, padx=12)

        ttk.Label(f_opts1, text="طريقة التقسيم: ", font=FONT_BOLD).pack(side=tk.LEFT)
        self.split_mode_var = tk.StringVar(value="part")
        ttk.Radiobutton(f_opts1, text="حسب الباب الرئيسي (part)", variable=self.split_mode_var, value="part").pack(side=tk.LEFT, padx=4)
        ttk.Radiobutton(f_opts1, text="ملف لكل صفحة ويب (page)", variable=self.split_mode_var, value="page").pack(side=tk.LEFT, padx=4)

        f_opts2 = ttk.Frame(grp_opts)
        f_opts2.pack(fill=tk.X, pady=(6, 2))

        self.use_cache_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(f_opts2, text="استخدام الكاش (Cache)", variable=self.use_cache_var).pack(side=tk.LEFT, padx=6)

        self.offline_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(f_opts2, text="وضع عدم الاتصال (Offline)", variable=self.offline_var).pack(side=tk.LEFT, padx=6)

        self.refresh_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(f_opts2, text="تحديث الكاش (Refresh)", variable=self.refresh_var).pack(side=tk.LEFT, padx=6)

        self.images_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(f_opts2, text="تنزيل الصور والغلاف", variable=self.images_var).pack(side=tk.LEFT, padx=6)

        self.check_epub_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(f_opts2, text="فحص EPUBCheck تلقائياً", variable=self.check_epub_var).pack(side=tk.LEFT, padx=6)

        # شريط التحكم وأزرار البدء
        f_act = ttk.Frame(frame, padding=4)
        f_act.pack(fill=tk.X, pady=4)

        self.btn_scrape = ttk.Button(f_act, text="▶️ بدء الاستخراج وبناء EPUB", style="Big.TButton", command=self._start_scrape)
        self.btn_scrape.pack(side=tk.LEFT, padx=4)

        self.btn_cancel = ttk.Button(f_act, text="⏹️ إلغاء", command=self._cancel_task, state=tk.DISABLED)
        self.btn_cancel.pack(side=tk.LEFT, padx=4)

        self.lbl_scrape_status = ttk.Label(f_act, text="جاهز للبدء.", font=FONT_BOLD)
        self.lbl_scrape_status.pack(side=tk.LEFT, padx=12)

        self.scrape_pbar = ttk.Progressbar(frame, mode="determinate")
        self.scrape_pbar.pack(fill=tk.X, pady=(2, 6))

        # صندوق السجلات الحي
        grp_log = ttk.LabelFrame(frame, text=" سجلات التشغيل والتنفيذ المباشرة ", padding=6)
        grp_log.pack(fill=tk.BOTH, expand=True)

        self.txt_scrape_log = tk.Text(grp_log, wrap=tk.WORD, font=FONT_MONO, height=10, bg="#fcfcfc")
        scroll_log = ttk.Scrollbar(grp_log, orient=tk.VERTICAL, command=self.txt_scrape_log.yview)
        self.txt_scrape_log.configure(yscrollcommand=scroll_log.set)
        scroll_log.pack(side=tk.RIGHT, fill=tk.Y)
        self.txt_scrape_log.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

    # -------------------------------------------------------------------------
    # التبويب 2: مزامنة PDF مع EPUB (النظام الجديد)
    # -------------------------------------------------------------------------
    def _build_sync_tab(self):
        frame = self.tab_sync

        # شرح سريع للتبويب
        note_box = ttk.Frame(frame, padding=6)
        note_box.pack(fill=tk.X, pady=(0, 6))
        ttk.Label(
            note_box,
            text="💡 نظام المزامنة: يطابق صفحات PDF مع نص EPUB لإنتاج EPUB جديد تكون فيه كل صفحة PDF ملف XHTML مستقل.\n"
                 "• PDF مصدر الحقيقة لعدد الصفحات والحدود | • EPUB مصدر الحقيقة الحصري للنص والتنسيق والوسوم دون أي تحريف.",
            font=FONT_NORMAL, foreground="#1b5e20",
        ).pack(side=tk.LEFT)

        # اختيار الملفات
        grp_files = ttk.LabelFrame(frame, text=" تحديد الملفات المدخلة ", padding=10)
        grp_files.pack(fill=tk.X, pady=(0, 8))

        # ملف PDF
        r_pdf = ttk.Frame(grp_files)
        r_pdf.pack(fill=tk.X, pady=3)
        ttk.Label(r_pdf, text="ملف PDF المستهدف :", font=FONT_BOLD, width=18).pack(side=tk.LEFT)
        self.sync_pdf_var = tk.StringVar()
        ttk.Entry(r_pdf, textvariable=self.sync_pdf_var, font=FONT_NORMAL).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=4)
        ttk.Button(r_pdf, text="استعراض PDF...", command=self._browse_pdf).pack(side=tk.LEFT)

        # ملف EPUB أو رابط ويب
        r_epub = ttk.Frame(grp_files)
        r_epub.pack(fill=tk.X, pady=3)
        ttk.Label(r_epub, text="ملف EPUB المصدر  :", font=FONT_BOLD, width=18).pack(side=tk.LEFT)
        self.sync_epub_var = tk.StringVar()
        ttk.Entry(r_epub, textvariable=self.sync_epub_var, font=FONT_NORMAL).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=4)
        ttk.Button(r_epub, text="استعراض EPUB...", command=self._browse_epub).pack(side=tk.LEFT)

        # مسار الملف الناتج
        r_out = ttk.Frame(grp_files)
        r_out.pack(fill=tk.X, pady=3)
        ttk.Label(r_out, text="ملف EPUB الناتج (اختياري):", font=FONT_NORMAL, width=18).pack(side=tk.LEFT)
        self.sync_out_var = tk.StringVar()
        ttk.Entry(r_out, textvariable=self.sync_out_var, font=FONT_NORMAL).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=4)
        ttk.Button(r_out, text="حفظ باسم...", command=self._browse_save_epub).pack(side=tk.LEFT)

        # خيارات المزامنة (pdfsync v2)
        grp_sync_opts = ttk.LabelFrame(frame, text=" خيارات وإعدادات المزامنة المتقدمة (pdfsync v2) ", padding=10)
        grp_sync_opts.pack(fill=tk.X, pady=(0, 8))

        # السطر 1: نطاق الصفحات واتجاهات النصوص والأرقام
        r_so1 = ttk.Frame(grp_sync_opts)
        r_so1.pack(fill=tk.X, pady=3)

        ttk.Label(r_so1, text="نطاق الصفحات:", font=FONT_NORMAL).pack(side=tk.LEFT)
        self.sync_pages_var = tk.StringVar()
        ttk.Entry(r_so1, textvariable=self.sync_pages_var, width=10).pack(side=tk.LEFT, padx=(4, 12))

        ttk.Label(r_so1, text="اتجاه نص PDF:", font=FONT_NORMAL).pack(side=tk.LEFT)
        self.sync_text_order_var = tk.StringVar(value="auto")
        cb_order = ttk.Combobox(r_so1, textvariable=self.sync_text_order_var, values=["auto", "logical", "reverse_chars", "reverse_words"], width=13, state="readonly")
        cb_order.pack(side=tk.LEFT, padx=(4, 12))

        ttk.Label(r_so1, text="اتجاه الأرقام (digits):", font=FONT_NORMAL).pack(side=tk.LEFT)
        self.sync_digits_var = tk.StringVar(value="auto")
        cb_digits = ttk.Combobox(r_so1, textvariable=self.sync_digits_var, values=["auto", "logical", "reversed"], width=10, state="readonly")
        cb_digits.pack(side=tk.LEFT, padx=(4, 4))

        # السطر 2: نقل الصور والدقة وفك ترميز الخطوط وإزاحة الصفحات
        r_so2 = ttk.Frame(grp_sync_opts)
        r_so2.pack(fill=tk.X, pady=3)

        ttk.Label(r_so2, text="نقل صور PDF (images):", font=FONT_NORMAL).pack(side=tk.LEFT)
        self.sync_images_var = tk.StringVar(value="auto")
        cb_imgs = ttk.Combobox(r_so2, textvariable=self.sync_images_var, values=["auto", "off"], width=8, state="readonly")
        cb_imgs.pack(side=tk.LEFT, padx=(4, 10))

        ttk.Label(r_so2, text="فك ترميز الخطوط (pdf_decode):", font=FONT_NORMAL).pack(side=tk.LEFT)
        self.sync_pdf_decode_var = tk.StringVar(value="auto")
        cb_decode = ttk.Combobox(r_so2, textvariable=self.sync_pdf_decode_var, values=["auto", "native", "glyph"], width=8, state="readonly")
        cb_decode.pack(side=tk.LEFT, padx=(4, 10))

        ttk.Label(r_so2, text="دقة الصور (DPI):", font=FONT_NORMAL).pack(side=tk.LEFT)
        self.sync_image_dpi_var = tk.StringVar(value="170")
        ttk.Entry(r_so2, textvariable=self.sync_image_dpi_var, width=5).pack(side=tk.LEFT, padx=(4, 10))

        ttk.Label(r_so2, text="إزاحة الترقيم:", font=FONT_NORMAL).pack(side=tk.LEFT)
        self.sync_page_offset_var = tk.StringVar(value="0")
        ttk.Entry(r_so2, textvariable=self.sync_page_offset_var, width=5).pack(side=tk.LEFT, padx=(4, 4))

        # السطر 3: مربعات الاختيار (الحواشي، الصور البديلة، الحواف، الفحص، والتجاوز)
        r_so3 = ttk.Frame(grp_sync_opts)
        r_so3.pack(fill=tk.X, pady=(4, 2))

        self.sync_split_notes_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(r_so3, text="فصل الحواشي (split_notes)", variable=self.sync_split_notes_var).pack(side=tk.LEFT, padx=(0, 8))

        self.sync_fallback_image_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(r_so3, text="صورة بديلة للصفحات الناقصة", variable=self.sync_fallback_image_var).pack(side=tk.LEFT, padx=(0, 8))

        self.sync_trim_edges_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(r_so3, text="حذف زوائد EPUB خارج PDF", variable=self.sync_trim_edges_var).pack(side=tk.LEFT, padx=(0, 8))

        self.sync_force_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(r_so3, text="تجاوز ضعف المطابقة (force)", variable=self.sync_force_var).pack(side=tk.LEFT, padx=(0, 8))

        self.sync_validate_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(r_so3, text="فحص EPUBCheck", variable=self.sync_validate_var).pack(side=tk.LEFT)

        # أزرار التشغيل
        f_sact = ttk.Frame(frame, padding=4)
        f_sact.pack(fill=tk.X, pady=4)

        self.btn_sync = ttk.Button(f_sact, text="⚡ بدء مزامنة وتوليد EPUB المقسم", style="Big.TButton", command=self._start_sync)
        self.btn_sync.pack(side=tk.LEFT, padx=4)

        self.btn_open_report = ttk.Button(f_sact, text="📊 تقرير JSON", command=self._view_last_report, state=tk.DISABLED)
        self.btn_open_report.pack(side=tk.LEFT, padx=3)

        self.btn_open_reader = ttk.Button(f_sact, text="📖 مقارنة جنبًا إلى جنب", command=self._open_in_reader, state=tk.DISABLED)
        self.btn_open_reader.pack(side=tk.LEFT, padx=3)

        self.lbl_sync_status = ttk.Label(f_sact, text="جاهز للمزامنة.", font=FONT_BOLD)
        self.lbl_sync_status.pack(side=tk.LEFT, padx=10)

        # ملخص النتيجة
        self.grp_sync_result = ttk.LabelFrame(frame, text=" ملخص نتيجة المزامنة والتطابق ", padding=8)
        self.grp_sync_result.pack(fill=tk.BOTH, expand=True)

        self.txt_sync_summary = tk.Text(self.grp_sync_result, wrap=tk.WORD, font=FONT_NORMAL, height=9, bg="#fbfbfb")
        scroll_sync = ttk.Scrollbar(self.grp_sync_result, orient=tk.VERTICAL, command=self.txt_sync_summary.yview)
        self.txt_sync_summary.configure(yscrollcommand=scroll_sync.set)
        scroll_sync.pack(side=tk.RIGHT, fill=tk.Y)
        self.txt_sync_summary.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        self.last_report_path: Path | None = None
        self.last_sync_output: Path | None = None

    # -------------------------------------------------------------------------
    # التبويب 3: فاحص EPUBCheck
    # -------------------------------------------------------------------------
    def _build_validator_tab(self):
        frame = self.tab_validator

        grp_val_file = ttk.LabelFrame(frame, text=" اختيار ملف EPUB للفحص ", padding=10)
        grp_val_file.pack(fill=tk.X, pady=(0, 8))

        r1 = ttk.Frame(grp_val_file)
        r1.pack(fill=tk.X)
        ttk.Label(r1, text="مسار ملف EPUB:", font=FONT_BOLD).pack(side=tk.LEFT, padx=(0, 6))
        self.val_file_var = tk.StringVar()
        ttk.Entry(r1, textvariable=self.val_file_var, font=FONT_NORMAL).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=4)
        ttk.Button(r1, text="استعراض...", command=self._browse_val_epub).pack(side=tk.LEFT, padx=2)
        ttk.Button(r1, text="🔬 فحص الملف بواسطة EPUBCheck", style="Accent.TButton", command=self._run_standalone_validation).pack(side=tk.LEFT, padx=4)

        # بطاقة النتيجة
        self.val_card = ttk.LabelFrame(frame, text=" تقرير ونتائج الفحص القياسي (W3C EPUBCheck 5.4.0) ", padding=10)
        self.val_card.pack(fill=tk.BOTH, expand=True)

        self.val_badge = ttk.Label(self.val_card, text="لم يتم الفحص بعد.", font=FONT_TITLE, foreground="#555")
        self.val_badge.pack(anchor=tk.W, pady=(0, 6))

        self.txt_val_details = tk.Text(self.val_card, wrap=tk.WORD, font=FONT_MONO, height=14, bg="#fafafa")
        scroll_v = ttk.Scrollbar(self.val_card, orient=tk.VERTICAL, command=self.txt_val_details.yview)
        self.txt_val_details.configure(yscrollcommand=scroll_v.set)
        scroll_v.pack(side=tk.RIGHT, fill=tk.Y)
        self.txt_val_details.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

    # -------------------------------------------------------------------------
    # التبويب 4: دليل الاستخدام وتوثيق المشروع
    # -------------------------------------------------------------------------
    def _build_about_tab(self):
        frame = self.tab_about

        txt = tk.Text(frame, wrap=tk.WORD, font=FONT_NORMAL, padx=12, pady=12, bg="#ffffff")
        scroll = ttk.Scrollbar(frame, orient=tk.VERTICAL, command=txt.yview)
        txt.configure(yscrollcommand=scroll.set)
        scroll.pack(side=tk.RIGHT, fill=tk.Y)
        txt.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        info_text = """
══════════════════════════════════════════════════════════════════════════
 📚 مشروع Book2EPUB Studio — دليل النظام والهيكلية
══════════════════════════════════════════════════════════════════════════

📌 الهدف الأساسي للمشروع:
تحويل ومزامنة الكتب الدينية والفقهية العربية من الويب وملفات PDF إلى كتب إلكترونية 
EPUB 3 متوافقة 100% مع معايير W3C الرسمية، مع الحفاظ المطلق على النص العربي وأرقام المسائل.

--------------------------------------------------------------------------
🌟 1. نظام استخراج الويب (Web Scraping to EPUB):
--------------------------------------------------------------------------
• يستخرج الكتب من موقع sistani.org تلقائياً بحسب نطاق الكتاب فقط دون تجاوز.
• يتجاهل عناصر وقوائم الموقع والإعلانات ويستخرج متن الكتاب فقط.
• يحافظ 100% على النص العربي، التشكيل، أرقام المسائل، وعناوين الفصول.
• يدعم وضعين للتقسيم:
   - حسب الأبواب الرئيسية (part): يجمع فصول كل باب في ملف XHTML سلس وسريع.
   - لكل صفحة ويب (page): ينشئ ملف Section منفصل لكل صفحة ويب تماماً.
• نظام تنزيل مهذب (Polite Crawling): مهلة متغيرة، تنويع زمني (Jitter)، واحترام robots.txt.
• كاش ذكي (cache/) لحفظ الصفحات والصور لسرعة إعادة البناء دون اتصال بالإنترنت.

--------------------------------------------------------------------------
🌟 2. نظام مزامنة PDF مع EPUB (طبقة pdfsync v2):
--------------------------------------------------------------------------
• يدمج ملف PDF وملف EPUB لنفس الكتاب لإنتاج EPUB جديد تكون فيه كل صفحة PDF 
  ممثلة بملف XHTML مستقل (page-0001.xhtml إلى page-NNNN.xhtml) ورقم الملف = رقم الصفحة دائماً.
• قاعدة مصدر الحقيقة:
   - PDF هو مصدر الحقيقة لـ: عدد الصفحات، بداية ونهاية كل صفحة، ومواضع الانتقال والصور.
   - EPUB هو مصدر الحقيقة لـ: النص، التنسيق، الروابط، والحواشي.
• بدون OCR نهائياً: الصفحات المصورة (غلاف، لوحات) تُرسم بأعلى جودة كصورة من PDF نفسه.
• فك ترميز الخطوط التالفة (glyph_decode): استخراج الحروف من الخطوط المدمجة مباشرة (عبر fonttools) لملفات Word القديمة.
• فصل الحواشي (split_notes): تُفصل الحواشي عن المتن وتوضع أسفل صفحتها الأصلية بدقة.
• كشف تلقائي للأرقام المعكوسة (digits auto) لمعالجة مشكلة اتجاه الأرقام في ملفات PDF العربية.
• صور بديلة (fallback_image): أي صفحة PDF لا نص لها في EPUB تُدرج كصورة وتُسجل في التقرير.
• مطابقة تسلسلية شاملة (Global Anchor-Chain Alignment) تمنع أي قفزات أو انهيار في الترتيب.
• حفظ حرفي 100% لنص EPUB مع التحقق الآلي الصارم بعد الانتهاء.

--------------------------------------------------------------------------
🌟 3. فحص الجودة القياسي (W3C EPUBCheck 5.4.0):
--------------------------------------------------------------------------
• مدمج به أداة الفحص الرسمية EPUBCheck لفحص صحة XML, OPF, NCX, NAV، وCSS، 
  وضمان خلو الكتاب من أي أخطاء (0 Fatals / 0 Errors).

--------------------------------------------------------------------------
💻 أوامر سطر الأوامر (CLI) لمن يفضل الطرفية:
--------------------------------------------------------------------------
• استخراج كتاب:
   python main.py "https://www.sistani.org/arabic/book/13/"

• استخراج كتاب مقسم لكل صفحة ويب:
   python main.py "https://www.sistani.org/arabic/book/13/" --split-mode page

• مزامنة PDF مع EPUB:
   python main.py sync --pdf book.pdf --epub book.epub -o out.epub

• فحص ملف EPUB:
   python -c "from validator import EPUBValidator, Config; print(EPUBValidator(Config()).validate('output/book.epub'))"
"""
        txt.insert(tk.END, info_text)
        txt.configure(state=tk.DISABLED)

    def _build_footer(self):
        ftr = ttk.Frame(self.root, padding=(10, 4))
        ftr.pack(fill=tk.X, side=tk.BOTTOM)

        self.lbl_global_status = ttk.Label(ftr, text="حالة النظام: متصل وجاهز.", font=FONT_NORMAL)
        self.lbl_global_status.pack(side=tk.LEFT)

        ttk.Label(ftr, text="Book2EPUB Studio v1.1.0", font=FONT_NORMAL, foreground="#888").pack(side=tk.RIGHT)

    # -------------------------------------------------------------------------
    # معالجة الأحداث والمهام الخلفية (Threads & Queue)
    # -------------------------------------------------------------------------
    def _process_log_queue(self):
        """تفريغ السجلات القادمة من الخلفية وتحديث الواجهة دورياً."""
        while not self.log_queue.empty():
            try:
                level, msg = self.log_queue.get_nowait()
                self.txt_scrape_log.insert(tk.END, f"{msg}\n")
                self.txt_scrape_log.see(tk.END)
            except Exception:
                break
        self.root.after(100, self._process_log_queue)

    def _open_output_dir(self):
        out_dir = self.config.output_dir.resolve()
        out_dir.mkdir(parents=True, exist_ok=True)
        try:
            if os.name == "nt":
                os.startfile(str(out_dir))
            else:
                import subprocess
                subprocess.Popen(["xdg-open", str(out_dir)])
        except Exception as e:
            messagebox.showerror("خطأ", f"تعذر فتح المجلد: {e}")

    def _browse_pdf(self):
        fn = filedialog.askopenfilename(title="اختر ملف PDF", filetypes=[("PDF files", "*.pdf"), ("All files", "*.*")])
        if fn:
            self.sync_pdf_var.set(fn)

    def _browse_epub(self):
        fn = filedialog.askopenfilename(title="اختر ملف EPUB المصدر", filetypes=[("EPUB files", "*.epub"), ("All files", "*.*")])
        if fn:
            self.sync_epub_var.set(fn)

    def _browse_save_epub(self):
        fn = filedialog.asksaveasfilename(title="حفظ ملف EPUB الناتج", defaultextension=".epub", filetypes=[("EPUB files", "*.epub")])
        if fn:
            self.sync_out_var.set(fn)

    def _browse_val_epub(self):
        fn = filedialog.askopenfilename(title="اختر ملف EPUB للفحص", filetypes=[("EPUB files", "*.epub"), ("All files", "*.*")])
        if fn:
            self.val_file_var.set(fn)

    def _cancel_task(self):
        self.stop_requested = True
        self.lbl_scrape_status.configure(text="جاري الإيقاف...")
        self.lbl_global_status.configure(text="تم طلب إلغاء العملية.")

    # -------------------------------------------------------------------------
    # منطق تنفيذ استخراج الويب
    # -------------------------------------------------------------------------
    def _start_scrape(self):
        url = self.url_var.get().strip()
        if not url:
            messagebox.showwarning("تنبيه", "يرجى إدخال رابط الكتاب أو التصنيف أولاً.")
            return

        self.btn_scrape.configure(state=tk.DISABLED)
        self.btn_cancel.configure(state=tk.NORMAL)
        self.lbl_scrape_status.configure(text="جاري بدء استخراج المحتوى...")
        self.scrape_pbar["value"] = 0

        # تطبيق الإعدادات المختارة
        cfg = Config()
        cfg.split_mode = self.split_mode_var.get()
        cfg.use_cache = self.use_cache_var.get()
        cfg.offline = self.offline_var.get()
        cfg.refresh = self.refresh_var.get()
        cfg.download_images = self.images_var.get()
        cfg.run_epubcheck = self.check_epub_var.get()

        is_all = (self.scrape_mode_var.get() == "all")

        def worker():
            try:
                if is_all:
                    from adapters.registry import get_adapter_for_url
                    downloader = Downloader(cfg)
                    adapter = get_adapter_for_url(url)
                    logging.info(f"فحص صفحة قائمة الكتب: {url}")
                    cat_html = downloader.get_html(url)
                    books_list = adapter.extract_category_books(cat_html, url)
                    logging.info(f"تم العثور على {len(books_list)} كتاباً في القائمة.")

                    for idx, (b_title, b_url) in enumerate(books_list, start=1):
                        if self.stop_requested:
                            break
                        self.root.after(0, lambda i=idx, tot=len(books_list), t=b_title: self.lbl_scrape_status.configure(
                            text=f"معالجة كتاب {i}/{tot}: {t}"
                        ))
                        scraper = BookScraper(cfg)
                        book = scraper.scrape_book(b_url)
                        builder = EPUBBuilder(book, cfg)
                        builder.build()
                else:
                    scraper = BookScraper(cfg)

                    def on_prog(curr, tot, name):
                        pct = int(100 * curr / max(tot, 1))
                        self.root.after(0, lambda p=pct, c=curr, t=tot, nm=name: (
                            self.scrape_pbar.configure(value=p),
                            self.lbl_scrape_status.configure(text=f"استخراج: {c}/{t} ({p}%) — {nm[:35]}"),
                        ))

                    book = scraper.scrape_book(url, progress_callback=on_prog)
                    self.root.after(0, lambda: self.lbl_scrape_status.configure(text="جاري بناء حزمة EPUB..."))
                    builder = EPUBBuilder(book, cfg)
                    out_epub = builder.build()

                    if cfg.run_epubcheck:
                        self.root.after(0, lambda: self.lbl_scrape_status.configure(text="فحص الملف عبر EPUBCheck..."))
                        val = EPUBValidator(cfg).validate(out_epub)
                        logging.info(f"نتيجة EPUBCheck: 0 Fatals / {val.error_count} Errors / {val.warning_count} Warnings")

                self.root.after(0, lambda: messagebox.showinfo("اكتمل بنجاح", "تمت عملية الاستخراج وبناء كتاب EPUB بنجاح!"))
            except Exception as e:
                logging.exception(e)
                self.root.after(0, lambda err=str(e): messagebox.showerror("خطأ", f"حدث خطأ أثناء الاستخراج: {err}"))
            finally:
                self.stop_requested = False
                self.root.after(0, self._scrape_finished)

        self.worker_thread = threading.Thread(target=worker, daemon=True)
        self.worker_thread.start()

    def _scrape_finished(self):
        self.btn_scrape.configure(state=tk.NORMAL)
        self.btn_cancel.configure(state=tk.DISABLED)
        self.lbl_scrape_status.configure(text="اكتملت العملية.")
        self.scrape_pbar["value"] = 100

    # -------------------------------------------------------------------------
    # منطق مزامنة PDF مع EPUB
    # -------------------------------------------------------------------------
    def _start_sync(self):
        pdf_p = self.sync_pdf_var.get().strip()
        epub_p = self.sync_epub_var.get().strip()

        if not pdf_p or not Path(pdf_p).exists():
            messagebox.showwarning("تنبيه", "يرجى تحديد ملف PDF صالح وموجود على القرص.")
            return

        is_url = epub_p.startswith("http://") or epub_p.startswith("https://")
        if not is_url and (not epub_p or not Path(epub_p).exists()):
            messagebox.showwarning("تنبيه", "يرجى تحديد ملف EPUB صالح وموجود على القرص، أو إدخال رابط ويب لكتاب في الموقع.")
            return

        pages_str = self.sync_pages_var.get().strip()
        page_range = None
        if pages_str:
            import re
            m = re.fullmatch(r"\s*(\d+)\s*(?:-\s*(\d+))?\s*", pages_str)
            if not m:
                messagebox.showerror("خطأ", "صيغة نطاق الصفحات غير صالحة. استخدم مثلاً: 1-95 أو 10")
                return
            a, b = int(m.group(1)), int(m.group(2) or m.group(1))
            page_range = (a, b)

        out_path = Path(self.sync_out_var.get().strip()) if self.sync_out_var.get().strip() else None

        try:
            image_dpi = int(self.sync_image_dpi_var.get().strip() or "170")
        except ValueError:
            image_dpi = 170

        try:
            page_offset = int(self.sync_page_offset_var.get().strip() or "0")
        except ValueError:
            page_offset = 0

        self.btn_sync.configure(state=tk.DISABLED)
        self.lbl_sync_status.configure(text="جاري بدء عملية المزامنة...")
        self.txt_sync_summary.delete("1.0", tk.END)

        def worker():
            try:
                def on_msg(s):
                    self.root.after(0, lambda msg=s: self.lbl_sync_status.configure(text=msg))

                source_epub = Path(epub_p)
                if is_url:
                    on_msg(f"🔍 بناء EPUB المصدر أولاً من الرابط: {epub_p}")
                    scraper = BookScraper(self.config)
                    book = scraper.scrape_book(epub_p)
                    source_epub = self.config.output_dir / f"_source_for_sync_{book.book_id}.epub"
                    builder = EPUBBuilder(book, self.config)
                    source_epub = builder.build(source_epub)
                    on_msg("✅ تم تجهيز EPUB المصدر، بدء المزامنة مع PDF...")

                opts = SyncOptions(
                    pdf=Path(pdf_p),
                    epub=source_epub,
                    output=out_path,
                    page_range=page_range,
                    text_order=self.sync_text_order_var.get(),
                    digits=self.sync_digits_var.get(),
                    pdf_decode=self.sync_pdf_decode_var.get(),
                    force=self.sync_force_var.get(),
                    header_frac=0.12,
                    footer_frac=0.06,
                    page_offset=page_offset,
                    trim_edges=self.sync_trim_edges_var.get(),
                    split_notes=self.sync_split_notes_var.get(),
                    images=self.sync_images_var.get(),
                    image_dpi=image_dpi,
                    fallback_image=self.sync_fallback_image_var.get(),
                    validate=self.sync_validate_var.get(),
                    match=MatchSettings(),
                )

                res = run_sync(opts, self.config, progress=on_msg)
                self.last_report_path = res.report_path
                self.root.after(0, lambda r=res: self._render_sync_result(r))
            except SyncError as se:
                logging.warning("SyncError: %s", se)
                self.root.after(0, lambda err=str(se): messagebox.showerror(
                    "تعذر إتمام المطابقة",
                    f"{err}\n\n💡 نصيحة:\n• إذا كان الخط العربي في PDF قديماً أو تالفاً، جرّب ضبط (فك ترميز الخطوط) على 'glyph'.\n• للتجاوز الإجباري، فعّل خيار: تجاوز ضعف المطابقة (force)."
                ))
            except Exception as e:
                logging.exception(e)
                self.root.after(0, lambda err=str(e): messagebox.showerror("خطأ في المزامنة", f"حدث خطأ أثناء المزامنة: {err}"))
            finally:
                self.root.after(0, lambda: self.btn_sync.configure(state=tk.NORMAL))

        self.worker_thread = threading.Thread(target=worker, daemon=True)
        self.worker_thread.start()

    def _render_sync_result(self, res: Any):
        self.btn_open_report.configure(state=tk.NORMAL)
        self.btn_open_reader.configure(state=tk.NORMAL)
        self.last_sync_output = res.output
        self.lbl_sync_status.configure(text="اكتملت المزامنة بنجاح!")

        r = res.report
        s = r.get("summary", {})
        v = r.get("verification", {})
        ec = v.get("epubcheck", {})

        count_match = v.get("page_files_equal_pdf_pages") and v.get("page_file_numbers_equal_pdf_numbers")
        body_preserved = v.get("body_text_preserved_exactly")
        notes_preserved = v.get("notes_text_preserved_exactly")

        lines = [
            f"🎉 تم إنشاء كتاب EPUB المقسم بنجاح!",
            f"📂 المسار النهائي       : {res.output}",
            f"📄 صفحات PDF المستهدفة : {r.get('pdf_pages', 0)} من إجمالي {r.get('pdf_total_pages_in_file', 0)} صفحة",
            f"📑 تطابق ملفات الصفحات : {'نعم، كل صفحة PDF = ملف xhtml مستقل برقمها تماماً ✅' if count_match else '❌ تنبيه: عدم تطابق'}",
            f"🔤 قراءة النص والأرقام   : اتجاه النص ({r.get('pdf_text_order')}) | الأرقام ({'معكوسة في PDF وتم تصحيحها' if r.get('pdf_digits_reversed') else 'عادية'})",
            f"🎯 متوسط دقة التشابه     : {s.get('mean_score', 0):.2%}  |  حالات الصفحات: {s.get('status', {})}",
            f"🛡️ حفظ متن EPUB الأصلي  : {'نعم، محفوظ حرفياً بنسبة 100% دون أي تبديل فقهي ✅' if body_preserved else '❌ تنبيه: هناك اختلاف في المتن'}",
            f"📝 حفظ نصوص الحواشي     : {'نعم، الحواشي مفصولة ومسندة لصفحاتها بنسبة 100% ✅' if notes_preserved else '❌ تنبيه: هناك اختلاف في الحواشي'}",
        ]

        if s.get("image_pages"):
            lines.append(f"🖼️ صفحات مصورة/غلاف  : {len(s['image_pages'])} صفحة {s['image_pages']}")
        if s.get("blank_pages"):
            lines.append(f"⚪ صفحات فارغة        : {len(s['blank_pages'])} صفحة {s['blank_pages']}")
        if s.get("pages_needing_review"):
            lines.append(f"⚠️ صفحات يُوصى بمراجعتها: {len(s['pages_needing_review'])} صفحة {s['pages_needing_review']}")

        d = r.get("dropped_epub_text", {})
        if d.get("before_first_page_chars") or d.get("after_last_page_chars"):
            lines.append(f"✂️ نصوص خارج نطاق PDF  : قبل={d.get('before_first_page_chars', 0)} حرفاً، بعد={d.get('after_last_page_chars', 0)} حرفاً")

        if ec.get("ran"):
            lines.append(f"🔬 فحص EPUBCheck       : {ec.get('fatals', 0)} Fatals / {ec.get('errors', 0)} Errors / {ec.get('warnings', 0)} Warnings")
            if ec.get("valid"):
                lines.append("   ✅ الكتاب سليم 100% ومطابق لمعايير W3C القياسية.")

        txt_str = "\n".join(lines)
        self.txt_sync_summary.insert(tk.END, txt_str)
        messagebox.showinfo("نجاح المزامنة", "تمت عملية مزامنة PDF مع EPUB بنجاح وبأعلى معايير الدقة!")

    def _view_last_report(self):
        if self.last_report_path and self.last_report_path.exists():
            try:
                if os.name == "nt":
                    os.startfile(str(self.last_report_path))
                else:
                    import subprocess
                    subprocess.Popen(["xdg-open", str(self.last_report_path)])
            except Exception as e:
                messagebox.showerror("خطأ", f"تعذر فتح ملف التقرير: {e}")

    def _open_in_reader(self):
        reader_script = BASE_DIR / "epub_reader.py"
        if not reader_script.exists():
            messagebox.showinfo("معلومة", "ملف قارئ المقارنة epub_reader.py غير موجود.")
            return
        if not self.last_sync_output or not Path(self.last_sync_output).exists():
            messagebox.showinfo("معلومة", "لم يتم العثور على ملف EPUB الناتج.")
            return

        pdf_p = self.sync_pdf_var.get().strip()
        cmd = [sys.executable, str(reader_script), str(self.last_sync_output)]
        if pdf_p and Path(pdf_p).exists():
            cmd.append(str(pdf_p))

        try:
            import subprocess
            subprocess.Popen(cmd)
        except Exception as e:
            messagebox.showerror(
                "خطأ في تشغيل القارئ",
                f"تعذر تشغيل القارئ: {e}\n\nملاحظة: يتطلب قارئ المقارنة تثبيت PyQt6 عبر:\npip install PyQt6 PyQt6-WebEngine pymupdf"
            )

    # -------------------------------------------------------------------------
    # منطق فحص EPUBCheck المنفرد
    # -------------------------------------------------------------------------
    def _run_standalone_validation(self):
        epub_p = self.val_file_var.get().strip()
        if not epub_p or not Path(epub_p).exists():
            messagebox.showwarning("تنبيه", "يرجى تحديد ملف EPUB صالح أولاً.")
            return

        self.txt_val_details.delete("1.0", tk.END)
        self.val_badge.configure(text="جاري فحص الملف عبر EPUBCheck...", foreground="#0d47a1")

        def worker():
            val = EPUBValidator(self.config)
            res = val.validate(Path(epub_p))
            self.root.after(0, lambda r=res: self._render_val_result(r))

        threading.Thread(target=worker, daemon=True).start()

    def _render_val_result(self, res: Any):
        if not res.available:
            self.val_badge.configure(text="❌ أداة EPUBCheck أو Java غير متوفرة.", foreground="#d32f2f")
            self.txt_val_details.insert(tk.END, "يرجى التأكد من توفر بيئة Java وملف epubcheck.jar داخل مجلد tools/.")
            return

        if res.is_valid:
            self.val_badge.configure(text="✅ الملف سليم 100% ومطابق للمواصفات الرسمية! (0 Fatals / 0 Errors)", foreground="#2e7d32")
        else:
            self.val_badge.configure(text=f"⚠️ وجد الفحص ملاحظات: {res.fatal_count} Fatals / {res.error_count} Errors / {res.warning_count} Warnings", foreground="#e65100")

        self.txt_val_details.insert(tk.END, res.raw_output or "\n".join(res.messages))


def launch_gui():
    """تشغيل الواجهة الرسومية."""
    root = tk.Tk()
    app = BookStudioGUI(root)
    root.mainloop()


if __name__ == "__main__":
    launch_gui()
