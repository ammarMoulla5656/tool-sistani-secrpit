"""
adapters/base.py — الصنف الأساسي التجريدي لمحولات المواقع (SiteAdapter).

يتيح هذا التصميم إضافة أي موقع آخر لاحقًا دون المساس ببقية النظام (Scraper, Builder, EPUB).
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from models import BookMeta, PageContent, TocEntry


class SiteAdapter(ABC):
    """الواجهة المشتركة لجميع محولات المواقع."""

    @classmethod
    @abstractmethod
    def can_handle(cls, url: str) -> bool:
        """هل هذا المحول يستطيع معالجة هذا الرابط؟"""
        ...

    @abstractmethod
    def extract_book_scope(self, url: str) -> tuple[str, str]:
        """استخراج معرّف الكتاب ونطاق روابط صفحاته.

        يعيد (book_id, scope_prefix).
        أي رابط لا يبدأ بـ scope_prefix يعتبر خارج الكتاب ولن يتم الزحف إليه.
        """
        ...

    @abstractmethod
    def extract_book_info(self, html: str, url: str) -> tuple[BookMeta, list[TocEntry]]:
        """استخراج البيانات الوصفية للكتاب وقائمة فصوله/صفحاته من صفحته الرئيسية."""
        ...

    @abstractmethod
    def extract_page_content(self, html: str, url: str) -> PageContent:
        """استخراج محتوى صفحة واحدة (العنوان والمتن والحواشي) فقط دون زوائد الموقع."""
        ...

    @abstractmethod
    def extract_next_url(self, html: str, current_url: str) -> str | None:
        """استخراج رابط الصفحة التالية إن وجد لمطابقة أو اكتشاف الصفحات المفقودة في الفهرس."""
        ...

    @abstractmethod
    def extract_category_books(self, html: str, url: str) -> list[tuple[str, str]]:
        """في حال تمرير صفحة قائمة كتب (مثل /arabic/book/fatwa/)، استخراج قائمة (title, book_url)."""
        ...
