"""
models.py — هياكل البيانات المشتركة بين مراحل البرنامج.

    Adapter  ──►  BookMeta + [TocEntry]          (اكتشاف الكتاب)
    Scraper  ──►  [Page] + [ImageAsset] = Book    (الاستخراج والتنظيف)
    Builder  ──►  EPUB                            (البناء)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


class ParseError(Exception):
    """الصفحة لا تحتوي البنية المتوقعة (مثلًا لا يوجد عنصر محتوى الكتاب)."""


class NoContentError(Exception):
    """الكتاب لا يحتوي صفحات نصية قابلة للاستخراج."""


@dataclass
class BookMeta:
    title: str
    author: str | None = None
    publisher: str | None = None
    language: str = "ar"
    direction: str = "rtl"
    source_name: str | None = None
    source_url: str = ""
    cover_url: str | None = None
    description: str | None = None
    extracted_at: datetime | None = None


@dataclass
class TocEntry:
    url: str
    path: list[str]                  # مسار العنوان، مثل ["أحكام الطهارة", "الوضوء"]
    discovered: bool = False         # True إذا اكتُشفت الصفحة عبر رابط "التالي" لا عبر الفهرس


@dataclass
class PageContent:
    """ما يعيده الـ Adapter من صفحة محتوى: عناصر bs4 خام قبل التنظيف."""
    title: str | None
    body: Any                        # bs4.Tag
    footnotes: Any | None = None     # bs4.Tag أو None


@dataclass
class Page:
    url: str
    path: list[str]
    blocks: list                     # IR نظيف (انظر parser.py)
    footnotes: list = field(default_factory=list)
    anchors: set[str] = field(default_factory=set)
    discovered: bool = False
    source_title: str | None = None


@dataclass
class ImageAsset:
    source_url: str
    filename: str                    # الاسم داخل OEBPS/Images
    media_type: str
    data: bytes


@dataclass
class FailedPage:
    url: str
    title: str
    error: str


@dataclass
class Book:
    meta: BookMeta
    pages: list[Page]
    images: list[ImageAsset] = field(default_factory=list)
    cover: ImageAsset | None = None
    failed: list[FailedPage] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    duplicates: list[tuple[str, str]] = field(default_factory=list)
    toc_count: int = 0               # عدد روابط فهرس الموقع
