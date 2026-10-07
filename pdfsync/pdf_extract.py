"""
pdfsync/pdf_extract.py — استخراج نص PDF مباشرةً عبر PyMuPDF (بدون OCR كمسار أساسي).

المسار الأساسي:   PDF -> PyMuPDF -> blocks/lines + إحداثيات -> تصنيف -> نص للمطابقة
المسار الاحتياطي: OCR فقط للصفحات المصورة بالكامل أو رديئة النص، وعند توفر Tesseract.

ما يُنتجه لكل صفحة:
  - PdfTextBlock: النص + الإحداثيات (x0,y0,x1,y1) + حجم الخط + نوع الكتلة
    (body | header | footer | page_number | footnote).
  - كشف تلقائي لنوع الصفحة: نصية (Text) أو مصورة (Scanned) أو رديئة الجودة.

PDF هو مصدر الحقيقة لعدد الصفحات وحدودها فقط؛ نصه لا يُكتب في الناتج النهائي.
"""
from __future__ import annotations

import hashlib
import logging
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

try:  # الاسم الجديد للحزمة
    import pymupdf as fitz
except ImportError:  # pragma: no cover
    import fitz  # type: ignore

from .normalize import normalize_for_match

logger = logging.getLogger("book2epub.pdfsync.pdf")

_ARABIC_RE = re.compile(r"[\u0600-\u06FF\u0750-\u077F\u08A0-\u08FF\uFB50-\uFDFF\uFE70-\uFEFF]")
_GARBAGE_RE = re.compile(r"[\uFFFD\uE000-\uF8FF\x00-\x08\x0B\x0C\x0E-\x1F]")


@dataclass
class PdfTextBlock:
    page_number: int
    lines: list[str]
    x0: float
    y0: float
    x1: float
    y1: float
    font_size: float = 0.0
    bold: bool = False
    kind: str = "body"      # body | header | footer | page_number | footnote
    text: str = ""          # النص بعد تطبيق ترتيب الأسطر المختار (logical/visual)

    def apply_order(self, mode: str) -> None:
        if mode == "reverse_chars":
            self.text = "\n".join(line[::-1] for line in self.lines)
        elif mode == "reverse_words":
            self.text = "\n".join(" ".join(reversed(line.split())) for line in self.lines)
        else:
            self.text = "\n".join(self.lines)


@dataclass
class PdfPage:
    number: int                      # رقم الصفحة في PDF (يبدأ من 1)
    width: float
    height: float
    blocks: list[PdfTextBlock] = field(default_factory=list)
    raw_char_count: int = 0
    image_coverage: float = 0.0      # نسبة مساحة الصور إلى مساحة الصفحة
    is_scanned: bool = False         # صفحة مصورة بلا نص قابل للاستخراج
    quality: float = 1.0             # 0..1 (نسبة الأحرف السليمة/العربية)
    ocr_used: bool = False
    notes: list[str] = field(default_factory=list)

    def match_text(self, include_footnotes: bool = False) -> str:
        """النص الذي يُستخدم في المطابقة (بدون الترويسة والتذييل ورقم الصفحة)."""
        kinds = {"body", "footnote"} if include_footnotes else {"body"}
        return "\n".join(b.text for b in self.blocks if b.kind in kinds and b.text)

    @property
    def has_text(self) -> bool:
        return self.raw_char_count > 0


@dataclass
class PdfDocument:
    path: Path
    total_pages: int                 # عدد صفحات الملف كاملًا
    pages: list[PdfPage]             # الصفحات المستهدفة (قد تكون مجموعة جزئية)
    metadata: dict = field(default_factory=dict)
    text_order: str = "logical"
    body_font_size: float = 0.0
    removed_headers: dict[str, int] = field(default_factory=dict)  # توقيع -> عدد الصفحات

    def set_text_order(self, mode: str) -> None:
        self.text_order = mode
        for page in self.pages:
            for block in page.blocks:
                block.apply_order(mode)


def _parse_blocks(page_dict: dict, page_number: int) -> tuple[list[PdfTextBlock], float]:
    """تحويل ناتج get_text('dict') إلى كتل نصية + مساحة الصور."""
    blocks: list[PdfTextBlock] = []
    img_area = 0.0
    for b in page_dict.get("blocks", []):
        x0, y0, x1, y1 = b["bbox"]
        if b.get("type") == 1:
            img_area += max(0.0, x1 - x0) * max(0.0, y1 - y0)
            continue
        lines: list[str] = []
        size_chars: list[tuple[float, int]] = []
        bold_chars = 0
        total_chars = 0
        for line in b.get("lines", []):
            spans = line.get("spans", [])
            text = "".join(s.get("text", "") for s in spans)
            if text.strip():
                lines.append(text)
            for s in spans:
                n = len(s.get("text", ""))
                size_chars.append((float(s.get("size", 0.0)), n))
                total_chars += n
                if int(s.get("flags", 0)) & 16:  # bit 4 = bold
                    bold_chars += n
        if not lines:
            continue
        weight = sum(n for _, n in size_chars) or 1
        avg_size = sum(sz * n for sz, n in size_chars) / weight
        blocks.append(PdfTextBlock(
            page_number=page_number, lines=lines,
            x0=x0, y0=y0, x1=x1, y1=y1,
            font_size=round(avg_size, 2),
            bold=bold_chars > total_chars * 0.5,
        ))
    return blocks, img_area


def _assess(page: PdfPage, expect_arabic: bool = True) -> None:
    """كشف تلقائي: هل الصفحة نصية سليمة أم مصورة أم رديئة الاستخراج؟"""
    text = "".join("".join(b.lines) for b in page.blocks)
    compact = re.sub(r"\s+", "", text)
    page.raw_char_count = len(compact)
    if not compact:
        page.quality = 0.0
        page.is_scanned = page.image_coverage >= 0.5
        return
    garbage = len(_GARBAGE_RE.findall(compact))
    page.quality = max(0.0, 1.0 - garbage / len(compact))
    if expect_arabic and len(compact) >= 40:
        letters = sum(1 for c in compact if c.isalpha())
        arabic = len(_ARABIC_RE.findall(compact))
        if letters >= 30 and arabic / letters < 0.15:
            page.quality = min(page.quality, 0.3)
            page.notes.append("نسبة الحروف العربية منخفضة جدًا")
    if len(compact) < 25 and page.image_coverage >= 0.6:
        page.is_scanned = True  # نص شبه معدوم + صورة تغطي الصفحة


def classify_blocks(
    doc: PdfDocument,
    header_frac: float = 0.07,
    footer_frac: float = 0.07,
    footnote_size_ratio: float = 0.86,
) -> None:
    """تصنيف الكتل: ترويسة/تذييل/رقم صفحة/حاشية/متن، اعتمادًا على الإحداثيات والتكرار."""
    pages = doc.pages
    n_pages = len(pages)

    # 1) مرشحو الترويسة والتذييل بحسب الموضع
    candidates: list[tuple[PdfPage, PdfTextBlock, str]] = []
    sig_pages: Counter[str] = Counter()
    for page in pages:
        seen: set[str] = set()
        for b in page.blocks:
            band = None
            if b.y1 <= page.height * header_frac:
                band = "header"
            elif b.y0 >= page.height * (1.0 - footer_frac):
                band = "footer"
            if band is None:
                continue
            sig = re.sub(r"\d+", "", normalize_for_match(" ".join(b.lines)))
            candidates.append((page, b, band))
            if sig and sig not in seen:
                seen.add(sig)
                sig_pages[sig] += 1

    min_repeat = max(3, int(0.15 * n_pages)) if n_pages > 3 else 2
    for page, b, band in candidates:
        norm = normalize_for_match(" ".join(b.lines))
        sig = re.sub(r"\d+", "", norm)
        if not sig:                              # أرقام فقط => رقم صفحة
            b.kind = "page_number"
        elif sig_pages[sig] >= min_repeat:       # نص متكرر عبر صفحات => ترويسة/تذييل جارية
            b.kind = band
            doc.removed_headers[sig] = sig_pages[sig]

    # 2) حجم خط المتن (الأكثر تكرارًا بوزن عدد الأحرف)
    size_weight: Counter[float] = Counter()
    for page in pages:
        for b in page.blocks:
            if b.kind == "body":
                size_weight[round(b.font_size * 2) / 2] += sum(len(x) for x in b.lines)
    doc.body_font_size = size_weight.most_common(1)[0][0] if size_weight else 0.0

    # 3) الحواشي: خط أصغر بوضوح من المتن وفي النصف السفلي من الصفحة
    if doc.body_font_size:
        limit = doc.body_font_size * footnote_size_ratio
        for page in pages:
            for b in page.blocks:
                if b.kind == "body" and b.font_size <= limit and b.y0 > page.height * 0.45:
                    b.kind = "footnote"


def _ocr_page(page, lang: str, dpi: int) -> dict | None:
    """OCR لصفحة واحدة عبر PyMuPDF+Tesseract إن توفرا؛ وإلا None."""
    try:
        tp = page.get_textpage_ocr(flags=0, language=lang, dpi=dpi, full=True)
        return page.get_text("dict", textpage=tp)
    except Exception as e:  # Tesseract/tessdata غير متوفرين
        logger.debug("OCR غير متاح: %s", e)
        return None


def extract_pdf(
    path: Path,
    page_range: tuple[int, int] | None = None,
    *,
    header_frac: float = 0.07,
    footer_frac: float = 0.07,
    ocr: str = "auto",          # auto | off | force
    ocr_lang: str = "ara",
    ocr_dpi: int = 300,
    ocr_cache_dir: Path | None = None,
    expect_arabic: bool = True,
) -> PdfDocument:
    """استخراج نص PDF صفحةً صفحة مع الإحداثيات والتصنيف.

    page_range: (أول صفحة، آخر صفحة) بترقيم يبدأ من 1 وشامل للطرفين.
    """
    path = Path(path)
    mdoc = fitz.open(str(path))
    total = len(mdoc)
    first, last = (1, total) if page_range is None else (max(1, page_range[0]), min(total, page_range[1]))
    if first > last:
        raise ValueError(f"نطاق الصفحات غير صالح: {page_range} (عدد صفحات الملف {total})")

    pdf_sha = None
    pages: list[PdfPage] = []
    for pno in range(first, last + 1):
        mp = mdoc[pno - 1]
        rect = mp.rect
        page = PdfPage(number=pno, width=float(rect.width), height=float(rect.height))
        d = mp.get_text("dict")
        blocks, img_area = _parse_blocks(d, pno)
        page.blocks = blocks
        page.image_coverage = img_area / max(1.0, rect.width * rect.height)
        _assess(page, expect_arabic)

        needs_ocr = ocr == "force" or (
            ocr == "auto" and (page.is_scanned or (page.has_text and page.quality < 0.5)
                               or (not page.has_text and page.image_coverage >= 0.5))
        )
        if needs_ocr:
            cached: dict | None = None
            cache_file = None
            if ocr_cache_dir is not None:
                if pdf_sha is None:
                    pdf_sha = hashlib.sha1(path.read_bytes()).hexdigest()[:16]
                cache_file = Path(ocr_cache_dir) / f"{pdf_sha}_{pno:05d}.json"
                if cache_file.exists():
                    import json
                    cached = json.loads(cache_file.read_text(encoding="utf-8"))
            od = cached or _ocr_page(mp, ocr_lang, ocr_dpi)
            if od is not None:
                if cache_file is not None and cached is None:
                    import json
                    cache_file.parent.mkdir(parents=True, exist_ok=True)
                    cache_file.write_text(json.dumps(od, ensure_ascii=False), encoding="utf-8")
                ocr_blocks, _ = _parse_blocks(od, pno)
                if ocr_blocks:
                    page.blocks = ocr_blocks
                    page.ocr_used = True
                    page.is_scanned = False
                    _assess(page, expect_arabic)
                    page.notes.append("استُخرج نص الصفحة عبر OCR")
            else:
                page.notes.append("الصفحة تحتاج OCR لكن Tesseract غير متوفر")
        pages.append(page)

    doc = PdfDocument(path=path, total_pages=total, pages=pages, metadata=dict(mdoc.metadata or {}))
    mdoc.close()
    classify_blocks(doc, header_frac, footer_frac)
    doc.set_text_order("logical")
    n_scanned = sum(1 for p in pages if p.is_scanned)
    logger.info("PDF: %d صفحة مستهدفة من %d | مصورة: %d | OCR: %d",
                len(pages), total, n_scanned, sum(1 for p in pages if p.ocr_used))
    return doc
