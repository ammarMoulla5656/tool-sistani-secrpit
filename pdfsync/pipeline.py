"""
pdfsync/pipeline.py — تنسيق مراحل مزامنة PDF مع EPUB.

    EPUB الأصلي ─► تيار المتن + تيار الحواشي (+ خرائط التطبيع) ─┐
                                                                ├─► محاذاة شاملة ─► حدود ─► قص DOM ─► EPUB جديد
    PDF ─► PyMuPDF (نص + بنية + صور) ───────────────────────────┘

PDF مصدر الحقيقة لعدد الصفحات وحدودها وصور صفحاته؛ EPUB مصدر الحقيقة للنص والتنسيق والروابط.
رقم ملف الصفحة = رقم صفحة PDF (page-0010.xhtml = صفحة 10 في PDF).
"""
from __future__ import annotations

import json
import logging
import re
from bisect import bisect_left
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .epub_source import SourceEpub, TocItem, read_epub
from .matcher import (MatchSettings, PageMatch, align_stream, choose_text_order, compute_boundaries,
                      page_texts_for, score_pages)
from .normalize import normalize_for_match
from .pdf_extract import PdfDocument, PdfPage, detect_reverse_digits, extract_pdf
from .splitter import PageAsset, PageFragment, PageSplitter, page_filename
from .toc import TocNode
from .writer import PageEPUBWriter

logger = logging.getLogger("book2epub.pdfsync")

_OPENERS = set("([{«“‘\"'\ufd3e\ufd3f")


@dataclass
class SyncOptions:
    pdf: Path
    epub: Path
    output: Path | None = None
    page_range: tuple[int, int] | None = None
    text_order: str = "auto"                 # auto | logical | reverse_chars | reverse_words
    digits: str = "auto"                     # auto | logical | reversed — اتجاه الأعداد متعددة الخانات في PDF
    header_frac: float = 0.12
    footer_frac: float = 0.06
    page_offset: int = 0                     # تسمية الصفحة في page-list = رقم صفحة PDF + page_offset
    trim_edges: bool | None = None           # None => True: تُحذف نصوص EPUB الواقعة خارج نطاق صفحات PDF
    split_notes: bool = True                 # فصل الحواشي (class=footnote) وإسنادها لصفحاتها
    note_classes: tuple[str, ...] = ()
    images: str = "auto"                     # auto | off — نقل صور صفحات PDF (بلا OCR)
    image_dpi: int = 170
    fallback_image: bool = True              # صفحة PDF نصها غير موجود في EPUB => تُضمَّن كصورة
    validate: bool = True
    report: Path | None = None
    match: MatchSettings = field(default_factory=MatchSettings)


@dataclass
class SyncResult:
    output: Path
    report: dict
    report_path: Path
    ok: bool


def _safe_filename(name: str) -> str:
    try:
        from urlutils import safe_filename  # type: ignore
        return safe_filename(name)
    except Exception:
        return re.sub(r'[\\/:*?"<>|\s]+', "_", name).strip("_") or "book"


# ------------------------------------------------------------------ مواضع القص

def _snap_back_openers(R: str, idx: int) -> int:
    k = idx
    while k > 0 and (R[k - 1] in _OPENERS or (R[k - 1].isspace() and k > 1 and R[k - 2] in _OPENERS)):
        k -= 1
    return k


def _snap_midword(R: str, idx: int, radius: int = 14) -> int:
    """إن وقع القص داخل كلمة نحرّكه لأقرب بداية كلمة."""
    if idx <= 0 or idx >= len(R) or R[idx - 1].isspace() or R[idx].isspace():
        return idx
    if not (R[idx - 1].isalnum() and R[idx].isalnum()):
        return idx
    for d in range(1, radius + 1):
        for cand in (idx - d, idx + d):
            if 0 < cand < len(R) and R[cand - 1].isspace() and not R[cand].isspace():
                return cand
    return idx


def _norm_to_raw(R: str, nmap, n_norm: int, b: int) -> int:
    return len(R) if b >= n_norm else int(nmap[b])


def _raw_cuts(R: str, nmap, n_norm: int, boundaries: list[int], matches: list[PageMatch]) -> list[int]:
    raw: list[int] = []
    prev = 0
    for m, b in zip(matches, boundaries):
        r = _norm_to_raw(R, nmap, n_norm, b)
        if m.pdf_chars > 0:
            r = _snap_midword(R, _snap_back_openers(R, r))
        r = max(prev, min(r, len(R)))
        raw.append(r)
        prev = r
    return raw


def _snap_to_starts(cuts: list[int], starts: list[int], radius: int = 25) -> list[int]:
    """قص الحواشي: إن قرُب القص من بداية حاشية (فقرة) التصق بها (حواشي الصفحة تبدأ بفقرة جديدة)."""
    if not starts:
        return cuts
    out: list[int] = []
    prev = 0
    for c in cuts:
        i = bisect_left(starts, c)
        best = c
        bd = radius + 1
        for j in (i - 1, i):
            if 0 <= j < len(starts) and abs(starts[j] - c) < bd:
                bd, best = abs(starts[j] - c), starts[j]
        best = max(prev, best)
        out.append(best)
        prev = best
    return out


def _extend_back_over_note_headers(cuts: list[int], starts: list[int], raw: str, pdf_note_texts: list[str],
                                   max_steps: int = 3) -> list[int]:
    """سطور حواشٍ قصيرة غير مرقّمة (مثل البسملة) تتكرر في PDF فلا تصلح مرتكزًا؛ نمدّ بداية الصفحة
    للخلف عليها إذا كان نص حواشي الصفحة في PDF يبدأ بها فعلًا."""
    out = list(cuts)
    for k, c in enumerate(out):
        head = pdf_note_texts[k]
        if not head or c not in starts:
            continue
        floor = next((out[j] for j in range(k - 1, -1, -1) if pdf_note_texts[j]), 0)
        i = starts.index(c)
        for _ in range(max_steps):
            if i == 0:
                break
            prev_start, prev_end = starts[i - 1], starts[i]
            if prev_start < floor:
                break
            seg = normalize_for_match(raw[prev_start:prev_end])
            if len(seg) < 8 or not head.startswith(seg):
                break
            head = head[len(seg):]
            out[k] = prev_start
            for j in range(k - 1, -1, -1):      # الصفحات الفارغة السابقة تتبع القص الجديد
                if out[j] > prev_start:
                    out[j] = prev_start
                else:
                    break
            i -= 1
    return out


def _last_text_end(matches: list[PageMatch], boundaries: list[int], n_norm: int) -> int:
    """نهاية آخر صفحة نصية في E (مُطبَّعة) عند القص الحدّي."""
    for k in range(len(matches) - 1, -1, -1):
        m = matches[k]
        if m.pdf_chars > 0 and m.status not in ("missing",):
            est = max(m.end or 0, min(n_norm, boundaries[k] + m.pdf_chars))
            return min(n_norm, est)
    return 0


# ------------------------------------------------------------------ الفهرس

def _build_toc(src: SourceEpub, splitter: PageSplitter, pages: list[PageFragment]) -> TocNode:
    ids = [p.ids for p in pages]
    root = TocNode(title="Root")
    counter = 0

    def conv(items: list[TocItem], parent: TocNode) -> None:
        nonlocal counter
        for it in items:
            counter += 1
            node = TocNode(title=it.title or "—", target_href=splitter.href_for(it.path, it.fragment, ids))
            parent.children[f"{counter}:{it.title}"] = node
            conv(it.children, node)

    items = src.toc
    if not items:
        items = [TocItem(d.title or Path(d.path).stem, d.path, None) for d in src.docs]
    conv(items, root)
    return root


# ------------------------------------------------------------------ صور الصفحات

def _body_chars(pg: PdfPage) -> list[tuple[float, int]]:
    return [(b.y1, len(re.sub(r"\s+", "", "".join(b.lines)))) for b in pg.blocks if b.kind == "body"]


def _make_assets(pdf: PdfDocument, matches: list[PageMatch], opts: SyncOptions) -> tuple[dict[int, list[PageAsset]], dict]:
    assets: dict[int, list[PageAsset]] = {}
    info: dict[int, dict] = {}
    if opts.images == "off":
        return assets, info
    by_page = {m.page: m for m in matches}
    for pg in pdf.pages:
        m = by_page.get(pg.number)
        mode = None
        if pg.kind == "image":
            mode = "image"
        elif pg.kind == "mixed":
            mode = "inline"
        elif opts.fallback_image and m is not None and m.status == "missing":
            mode = "fallback"
        if mode is None:
            continue
        label = f"صفحة {pg.number}"
        out: list[PageAsset] = []
        if mode == "image":
            rect = pg.image_region()
            if rect:
                area = (rect[2] - rect[0]) * (rect[3] - rect[1]) / max(1.0, pg.width * pg.height)
                data, ext = pdf.render(pg.number, rect, opts.image_dpi, prefer_jpeg=area > 0.5)
                out.append(PageAsset(f"pdf-page-{pg.number:04d}-1.{ext}", "image/jpeg" if ext == "jpg" else "image/png",
                                     data, label))
        elif mode == "inline":
            blocks = _body_chars(pg)
            total = sum(n for _, n in blocks) or 1
            for i, im in enumerate(pg.images, 1):
                before = sum(n for y1, n in blocks if y1 <= im.y0 + 2)
                data, ext = pdf.render(pg.number, im.rect, opts.image_dpi, prefer_jpeg=False)
                out.append(PageAsset(f"pdf-page-{pg.number:04d}-{i}.{ext}", "image/png", data, label, y_ratio=before / total))
        else:  # fallback: صفحة كاملة
            rect = (0.0, 0.0, pg.width, pg.height)
            data, ext = pdf.render(pg.number, rect, opts.image_dpi, prefer_jpeg=False)
            out.append(PageAsset(f"pdf-page-{pg.number:04d}-1.{ext}", "image/png", data, label))
            if m is not None:
                m.notes.append("نص الصفحة غير موجود في EPUB — أُدرجت صورة الصفحة من PDF بدلًا منه")
        if out:
            assets[pg.number] = out
            info[pg.number] = {"mode": mode, "images": [a.zip_name for a in out]}
    return assets, info


# ------------------------------------------------------------------ التشغيل

def run_sync(opts: SyncOptions, config=None, progress: Callable[[str], None] | None = None) -> SyncResult:
    say = progress or (lambda s: None)
    st = opts.match

    say("قراءة EPUB وفهرسة النص (متن + حواشٍ)...")
    src = read_epub(opts.epub, split_notes=opts.split_notes, note_classes=opts.note_classes)
    if not src.docs:
        raise ValueError("لا يحتوي EPUB على مستندات محتوى قابلة للمعالجة")

    say("تحليل PDF (نص + بنية + صور) — بلا OCR...")
    pdf: PdfDocument = extract_pdf(opts.pdf, opts.page_range, header_frac=opts.header_frac, footer_frac=opts.footer_frac)
    try:
        return _run(opts, config, say, st, src, pdf)
    finally:
        pdf.close()


def _run(opts: SyncOptions, config, say, st: MatchSettings, src: SourceEpub, pdf: PdfDocument) -> SyncResult:
    n_pages = len(pdf.pages)
    has_notes = bool(src.note_norm)

    # ---- اتجاه النص والأرقام
    if opts.text_order == "auto":
        order, order_scores = choose_text_order(pdf, src.norm_text)
    else:
        pdf.set_text_order(opts.text_order)
        order, order_scores = opts.text_order, {}
    rev = detect_reverse_digits(pdf) if opts.digits == "auto" else (opts.digits == "reversed")
    pdf.set_reverse_digits(rev)
    logger.info("اتجاه النص: %s %s | الأرقام معكوسة: %s", order, order_scores, rev)

    # ---- تيار المتن
    say("محاذاة المتن على صفحات PDF...")
    if has_notes:
        body_texts = page_texts_for(pdf, "body")
    else:  # لا حواشي منفصلة في EPUB: حواشي PDF (إن وُجدت) جزء من النص المطابَق
        body_texts = [(p.number, normalize_for_match(p.body_text() + "\n" + p.note_text())) for p in pdf.pages]
    body_al = align_stream(body_texts, src.norm_text, st)
    bnorm = compute_boundaries(body_al.matches, len(src.norm_text))
    score_pages(body_al.matches, body_texts, bnorm, src.norm_text, st)
    bm = body_al.matches

    # ---- تيار الحواشي
    nm: list[PageMatch] = []
    nnorm: list[int] = [0] * n_pages
    if has_notes:
        say("محاذاة الحواشي على صفحاتها...")
        note_texts = page_texts_for(pdf, "note")
        note_al = align_stream(note_texts, src.note_norm, st)
        nnorm = compute_boundaries(note_al.matches, len(src.note_norm))
        score_pages(note_al.matches, note_texts, nnorm, src.note_norm, st)
        nm = note_al.matches
        if sum(len(t) for _, t in note_texts) < 0.05 * len(src.note_norm):
            logger.warning("حواشي PDF قليلة جدًا مقارنة بحواشي EPUB — قد يكون كشف منطقة الحواشي فاشلًا")

    # ---- الحدود الخام
    trim = True if opts.trim_edges is None else opts.trim_edges
    los = _raw_cuts(src.raw_text, src.norm_map, len(src.norm_text), bnorm, bm)
    his = los[1:] + [0]
    dropped_front = dropped_back = 0
    if trim:
        end_n = _last_text_end(bm, bnorm, len(src.norm_text))
        his[-1] = max(los[-1], _snap_midword(src.raw_text, _norm_to_raw(src.raw_text, src.norm_map, len(src.norm_text), end_n)))
        dropped_front, dropped_back = los[0], len(src.raw_text) - his[-1]
    else:
        los[0] = 0
        his[-1] = len(src.raw_text) + 1

    nlos = [0] * n_pages
    nhis = [0] * n_pages
    if has_notes:
        starts = sorted(d.spans[r][0] for d in src.docs for r in d.note_roots)
        nlos = _raw_cuts(src.note_raw, src.note_map, len(src.note_norm), nnorm, nm)
        nlos = _snap_to_starts(nlos, starts)
        nlos = _extend_back_over_note_headers(nlos, starts, src.note_raw, [t for _, t in note_texts])
        nhis = nlos[1:] + [0]
        if trim:
            end_n = _last_text_end(nm, nnorm, len(src.note_norm))
            nhis[-1] = max(nlos[-1], _norm_to_raw(src.note_raw, src.note_map, len(src.note_norm), end_n))
            nhis[-1] = _snap_to_starts([nhis[-1]], starts + [len(src.note_raw)], radius=40)[0]
        else:
            nlos[0] = 0
            nhis[-1] = len(src.note_raw) + 1

    for k, m in enumerate(bm):
        m.raw_start, m.raw_end = los[k], min(his[k], len(src.raw_text))

    # ---- صور الصفحات
    say("نقل صور الصفحات من PDF (بلا OCR)...")
    assets, asset_info = _make_assets(pdf, bm, opts)

    # ---- القص
    say("قص محتوى EPUB الأصلي إلى صفحات PDF...")
    numbers = [p.number for p in pdf.pages]
    labels = [str(n + opts.page_offset) for n in numbers]
    splitter = PageSplitter(src, los, his, nlos, nhis, numbers, language=src.metadata.get("language") or "ar",
                            page_labels=labels, page_assets=assets)
    pages = splitter.build_all()
    toc_root = _build_toc(src, splitter, pages)

    # ---- الثوابت
    body_slice = src.raw_text[los[0]:min(his[-1], len(src.raw_text))]
    body_ok = "".join(body_slice.split()) == "".join(splitter.total_body_text(pages).split())
    notes_ok = True
    if has_notes:
        note_slice = src.note_raw[nlos[0]:min(nhis[-1], len(src.note_raw))]
        notes_ok = "".join(note_slice.split()) == "".join(splitter.total_note_text(pages).split())
    count_ok = len(pages) == n_pages
    numbering_ok = [p.number for p in pages] == numbers
    if not body_ok:
        logger.error("فشل التحقق: نص المتن في الصفحات لا يطابق EPUB الأصلي!")
    if not notes_ok:
        logger.error("فشل التحقق: نص الحواشي في الصفحات لا يطابق EPUB الأصلي!")

    # ---- الكتابة
    out = opts.output
    if out is None:
        out_dir = Path(getattr(config, "output_dir", Path("."))) if config is not None else Path(".")
        out = out_dir / (_safe_filename(src.metadata.get("title") or opts.epub.stem) + " - pages.epub")
    say("كتابة EPUB النهائي...")
    writer = PageEPUBWriter(src.metadata.get("title") or opts.epub.stem)
    out = writer.write(out, src, pages, toc_root, labels,
                       source_note=f"نسخة مقسمة حسب صفحات PDF — EPUB: {opts.epub.name} | PDF: {opts.pdf.name}")

    # ---- EPUBCheck (اختياري)
    epubcheck: dict = {"ran": False}
    if opts.validate and (config is None or getattr(config, "run_epubcheck", True)):
        try:
            from validator import EPUBValidator  # type: ignore
            say("التحقق عبر EPUBCheck...")
            res = EPUBValidator(config).validate(out)
            epubcheck = {"ran": res.available, "valid": res.is_valid, "fatals": res.fatal_count,
                         "errors": res.error_count, "warnings": res.warning_count, "messages": res.messages[:20]}
        except Exception as e:  # pragma: no cover
            logger.info("EPUBCheck غير متاح: %s", e)

    # ---- التقرير
    report = _build_report(opts, src, pdf, out, order, order_scores, rev, trim, bm, nm, los, his, nlos, nhis,
                           labels, splitter, asset_info, body_ok, notes_ok, count_ok, numbering_ok, epubcheck,
                           dropped_front, dropped_back, has_notes)
    report_path = opts.report or out.with_suffix(".sync-report.json")
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    ok = body_ok and notes_ok and count_ok and numbering_ok and (not epubcheck.get("ran") or epubcheck.get("valid", False))
    return SyncResult(output=out, report=report, report_path=report_path, ok=ok)


def _build_report(opts, src, pdf, out, order, order_scores, rev, trim, bm, nm, los, his, nlos, nhis, labels,
                  splitter, asset_info, body_ok, notes_ok, count_ok, numbering_ok, epubcheck,
                  dropped_front, dropped_back, has_notes) -> dict:
    n_pages = len(pdf.pages)
    conf: dict[str, int] = {}
    for m in bm:
        conf[m.confidence] = conf.get(m.confidence, 0) + 1
    status: dict[str, int] = {}
    for m in bm:
        status[m.status] = status.get(m.status, 0) + 1
    scored = [m.score for m in bm if m.status in ("exact", "fuzzy", "partial")]
    note_by_page = {m.page: m for m in nm}
    pages_report = []
    for i, m in enumerate(bm):
        pg = pdf.pages[i]
        nmatch = note_by_page.get(m.page)
        head_pdf = normalize_for_match(pg.body_text())[:40]
        head_epub = normalize_for_match(src.raw_text[los[i]:min(his[i], len(src.raw_text))][:200])[:40]
        pages_report.append({
            "page": m.page, "label": labels[i], "file": f"Text/{page_filename(m.page)}",
            "pdf_kind": pg.kind, "status": m.status, "confidence": m.confidence, "score": m.score,
            "anchors": m.anchors_hit, "pdf_chars": m.pdf_chars,
            "epub_chars": max(0, min(his[i], len(src.raw_text)) - los[i]),
            "notes": {
                "status": nmatch.status if nmatch else None, "score": nmatch.score if nmatch else None,
                "epub_chars": max(0, min(nhis[i], len(src.note_raw)) - nlos[i]) if has_notes else 0,
            },
            "image": asset_info.get(m.page),
            "pdf_head": head_pdf, "epub_head": head_epub,
            "messages": m.notes + pdf.pages[i].notes + (nmatch.notes if nmatch else []),
        })
    problem_pages = [p["page"] for p in pages_report
                     if p["status"] in ("missing", "partial", "interpolated", "unmatched") or p["confidence"] == "low"]
    return {
        "pdf": str(opts.pdf), "epub": str(opts.epub), "output": str(out),
        "pdf_pages": n_pages, "pdf_total_pages_in_file": pdf.total_pages,
        "pdf_text_order": order, "pdf_text_order_scores": order_scores, "pdf_digits_reversed": rev,
        "epub_body_chars": len(src.raw_text), "epub_note_chars": len(src.note_raw),
        "trim_edges": trim,
        "dropped_epub_text": {"before_first_page_chars": dropped_front, "after_last_page_chars": dropped_back},
        "summary": {
            "confidence": conf, "status": status,
            "mean_score": round(sum(scored) / len(scored), 4) if scored else 0.0,
            "image_pages": [p.number for p in pdf.pages if p.kind == "image"],
            "blank_pages": [p.number for p in pdf.pages if p.kind == "blank"],
            "pages_needing_review": problem_pages,
        },
        "verification": {
            "page_files_equal_pdf_pages": count_ok,
            "page_file_numbers_equal_pdf_numbers": numbering_ok,
            "body_text_preserved_exactly": body_ok,
            "notes_text_preserved_exactly": notes_ok,
            "epubcheck": epubcheck,
        },
        "removed_running_headers": [{"signature": k, "pages": v} for k, v in pdf.removed_headers.items()],
        "unresolved_links": splitter.unresolved[:50],
        "pages": pages_report,
    }
