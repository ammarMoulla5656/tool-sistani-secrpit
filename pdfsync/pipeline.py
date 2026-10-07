"""
pdfsync/pipeline.py — تنسيق مراحل مزامنة PDF مع EPUB.

    EPUB الأصلي ──► النص الخام + خريطة التطبيع ─┐
                                                 ├─► مطابقة تسلسلية ─► حدود الصفحات ─► قص DOM ─► EPUB جديد
    PDF ──► PyMuPDF (نص + كتل + إحداثيات) ───────┘

PDF مصدر الحقيقة لعدد الصفحات وحدودها؛ EPUB مصدر الحقيقة للنص والتنسيق والروابط والصور.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from config import Config
from epub_builder import TocNode
from urlutils import safe_filename

from .epub_source import SourceEpub, TocItem, read_epub
from .matcher import (MatchSettings, PageMatch, choose_text_order, compute_boundaries, match_pages,
                      page_match_text)
from .pdf_extract import PdfDocument, extract_pdf
from .splitter import PageFragment, PageSplitter, page_filename
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
    ocr: str = "auto"                        # auto | off | force
    include_footnotes: bool = False
    header_frac: float = 0.07
    footer_frac: float = 0.07
    page_offset: int = 0                     # تسمية الصفحة = رقم صفحة PDF + page_offset
    trim_edges: bool | None = None           # None: مفعّل تلقائيًا عند تحديد نطاق جزئي
    validate: bool = True
    report: Path | None = None
    match: MatchSettings = field(default_factory=MatchSettings)


@dataclass
class SyncResult:
    output: Path
    report: dict
    report_path: Path
    ok: bool


# ------------------------------------------------------------------ مواضع القص

def _snap_back_openers(R: str, idx: int) -> int:
    while idx > 0 and R[idx - 1] in _OPENERS:
        idx -= 1
    return idx


def _snap_whitespace(R: str, idx: int, radius: int = 150) -> int:
    if idx <= 0 or idx >= len(R) or R[idx - 1].isspace():
        return idx
    for d in range(1, radius + 1):
        for cand in (idx - d, idx + d):
            if 0 < cand < len(R) and R[cand - 1].isspace() and not R[cand].isspace():
                return cand
    return idx


def _raw_cuts(src: SourceEpub, matches: list[PageMatch], boundaries: list[int]) -> list[int]:
    R, nmap, n_e = src.raw_text, src.norm_map, len(src.norm_text)
    raw: list[int] = []
    prev = 0
    for m, b in zip(matches, boundaries):
        r = len(R) if b >= n_e else int(nmap[b])
        if m.status == "interpolated":
            r = _snap_whitespace(R, r)
        else:
            r = _snap_back_openers(R, r)
        r = max(prev, r)
        raw.append(r)
        prev = r
    return raw


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
    if not items:  # لا فهرس في المصدر: فهرس بسيط من عناوين المستندات
        items = [TocItem(d.title or Path(d.path).stem, d.path, None) for d in src.docs]
    conv(items, root)
    return root


# ------------------------------------------------------------------ التقرير

def run_sync(opts: SyncOptions, config: Config | None = None,
             progress: Callable[[str], None] | None = None) -> SyncResult:
    config = config or Config()
    say = progress or (lambda s: None)
    st = opts.match
    st.include_footnotes = opts.include_footnotes

    say("قراءة EPUB وفهرسة النص...")
    src = read_epub(opts.epub)
    if not src.docs:
        raise ValueError("لا يحتوي EPUB على مستندات محتوى قابلة للمعالجة")

    say("استخراج نص PDF (PyMuPDF)...")
    pdf: PdfDocument = extract_pdf(
        opts.pdf, opts.page_range,
        header_frac=opts.header_frac, footer_frac=opts.footer_frac, ocr=opts.ocr,
        ocr_cache_dir=config.cache_dir / "pdf_ocr",
    )
    n_pages = len(pdf.pages)

    if opts.text_order == "auto":
        order, order_scores = choose_text_order(pdf, src.norm_text, st)
    else:
        pdf.set_text_order(opts.text_order)
        order, order_scores = opts.text_order, {}
    logger.info("اتجاه نص PDF المعتمد: %s %s", order, order_scores)

    say("مطابقة الصفحات تسلسليًا...")
    page_texts = [(p.number, page_match_text(p, st)) for p in pdf.pages]
    matches = match_pages(page_texts, src.norm_text, st)
    boundaries = compute_boundaries(matches, len(src.norm_text), st)

    trim = opts.trim_edges if opts.trim_edges is not None else (opts.page_range is not None)
    preamble_chars = src.norm_text and boundaries[0]
    if not trim:
        boundaries[0] = 0
    raw = _raw_cuts(src, matches, boundaries)

    los = list(raw)
    his = raw[1:] + [0]
    if trim:
        last = matches[-1]
        if last.end is not None and last.end < len(src.norm_text):
            his[-1] = int(src.norm_map[last.end])
        else:
            his[-1] = len(src.raw_text)
        his[-1] = max(his[-1], los[-1])
    else:
        los[0] = 0
        his[-1] = len(src.raw_text) + 1
    for k, m in enumerate(matches):
        m.boundary = boundaries[k]
        m.raw_start, m.raw_end = los[k], min(his[k], len(src.raw_text))

    say("قص محتوى EPUB الأصلي إلى صفحات...")
    labels = [str(p.number + opts.page_offset) for p in pdf.pages]
    splitter = PageSplitter(src, los, his, language=src.metadata.get("language") or "ar", page_labels=labels)
    pages = splitter.build_all()
    toc_root = _build_toc(src, splitter, pages)

    # ---- التحقق من الثوابت
    src_slice = src.raw_text[los[0]:min(his[-1], len(src.raw_text))]
    text_ok = "".join(src_slice.split()) == "".join(splitter.total_text(pages).split())
    count_ok = len(pages) == n_pages
    if not text_ok:
        logger.error("فشل التحقق: نص الصفحات الناتجة لا يطابق نص EPUB الأصلي!")

    # ---- الكتابة
    out = opts.output
    if out is None:
        out = config.output_dir / (safe_filename(src.metadata.get("title") or opts.epub.stem) + " - pages.epub")
    say("كتابة EPUB النهائي...")
    writer = PageEPUBWriter(config, src.metadata.get("title") or opts.epub.stem)
    out = writer.write(
        out, src, pages, toc_root, labels,
        source_note=f"نسخة مقسمة حسب صفحات PDF — EPUB: {opts.epub.name} | PDF: {opts.pdf.name}",
    )

    # ---- EPUBCheck
    epubcheck: dict = {"ran": False}
    if opts.validate and config.run_epubcheck:
        say("التحقق عبر EPUBCheck...")
        from validator import EPUBValidator
        res = EPUBValidator(config).validate(out)
        epubcheck = {"ran": res.available, "valid": res.is_valid, "fatals": res.fatal_count,
                     "errors": res.error_count, "warnings": res.warning_count,
                     "messages": res.messages[:20]}

    # ---- التقرير
    conf_count: dict[str, int] = {}
    for m in matches:
        conf_count[m.confidence] = conf_count.get(m.confidence, 0) + 1
    scored = [m.score for m in matches if m.status in ("exact", "fuzzy")]
    report = {
        "pdf": str(opts.pdf), "epub": str(opts.epub), "output": str(out),
        "pdf_pages": n_pages, "pdf_total_pages_in_file": pdf.total_pages,
        "pdf_text_order": order, "pdf_text_order_scores": order_scores,
        "epub_text_chars": len(src.raw_text),
        "front_matter_before_first_page_chars": int(preamble_chars or 0),
        "trim_edges": trim,
        "summary": {
            "confidence": conf_count,
            "mean_score": round(sum(scored) / len(scored), 4) if scored else 0.0,
            "scanned_pages": [p.number for p in pdf.pages if p.is_scanned],
            "ocr_pages": [p.number for p in pdf.pages if p.ocr_used],
        },
        "verification": {
            "page_files_equal_pdf_pages": count_ok,
            "text_preserved_exactly": text_ok,
            "epubcheck": epubcheck,
        },
        "removed_running_headers": [{"signature": k, "pages": v} for k, v in pdf.removed_headers.items()],
        "unresolved_links": splitter.unresolved[:50],
        "pages": [
            {
                "page": m.page, "label": labels[i], "file": f"Text/{page_filename(i + 1)}",
                "status": m.status, "confidence": m.confidence, "score": m.score,
                "anchors": f"{m.anchors_hit}/{m.anchors_total}",
                "pdf_chars": m.pdf_chars, "epub_chars": max(0, m.raw_end - m.raw_start),
                "notes": m.notes + pdf.pages[i].notes,
            }
            for i, m in enumerate(matches)
        ],
    }
    report_path = opts.report or out.with_suffix(".sync-report.json")
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    ok = text_ok and count_ok and (not epubcheck.get("ran") or epubcheck.get("valid", False))
    return SyncResult(output=out, report=report, report_path=report_path, ok=ok)
