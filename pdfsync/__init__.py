"""
pdfsync/__init__.py — طبقة مزامنة PDF مع EPUB.

تأخذ PDF (مصدر الحقيقة لحدود الصفحات) وEPUB (مصدر الحقيقة للنص والتنسيق)
وتنتج EPUB جديدًا يُمثَّل فيه كل صفحة PDF بملف XHTML مستقل: Text/page-NNNN.xhtml
"""
from __future__ import annotations

__all__ = ["run_sync"]


def run_sync(*args, **kwargs):  # استيراد كسول لتفادي كلفة تحميل PyMuPDF عند الحاجة فقط
    from .pipeline import run_sync as _run

    return _run(*args, **kwargs)
