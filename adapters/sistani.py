"""
adapters/sistani.py — محول خاص بموقع سماحة السيد السيستاني (sistani.org).

يتعامل بدقة مع هيكلية الموقع:
- الكتب تحت المسار: /arabic/book/<id>/
- الفهرس في: #main-book-content ul.baz > li > a
- تقسيم المسارات الهرمية عبر رمز الفاصل « »
- محتوى الصفحة في: div.book-text و div.book-footnote
- عنوان الصفحة في: h1.c
- الروابط التتابعية: a.fl (التالي) و a.fr (السابق)
"""
from __future__ import annotations

import re
from datetime import datetime
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup

from adapters.base import SiteAdapter
from models import BookMeta, NoContentError, PageContent, ParseError, TocEntry
from urlutils import normalize_url

_HOST_ALIASES = {
    "sistani.org": "www.sistani.org",
    "sistani.com": "www.sistani.org",
    "www.sistani.com": "www.sistani.org",
    "al-sistani.org": "www.sistani.org",
}

_BOOK_URL_PATTERN = re.compile(r"^/arabic/book/(\d+)/?(?:(\d+)/?)?$")


class SistaniAdapter(SiteAdapter):
    @classmethod
    def can_handle(cls, url: str) -> bool:
        host = (urlsplit(url).hostname or "").lower()
        return "sistani.org" in host or "sistani.com" in host

    def extract_book_scope(self, url: str) -> tuple[str, str]:
        """استخراج رقم الكتاب ونطاقه القانوني."""
        norm = normalize_url(url, host_aliases=_HOST_ALIASES)
        parts = urlsplit(norm)
        match = _BOOK_URL_PATTERN.match(parts.path)
        if not match:
            # ربما مسار لغة أخرى أو مسار غير قياسي
            m2 = re.search(r"/(arabic|persian|urdu|english)/book/(\d+)/?", parts.path)
            if m2:
                lang = m2.group(1)
                book_id = m2.group(2)
                scope = f"https://www.sistani.org/{lang}/book/{book_id}/"
                return book_id, scope
            raise ValueError(f"الرابط لا يبدو رابط كتاب في موقع sistani.org: {url}")

        book_id = match.group(1)
        scope = f"https://www.sistani.org/arabic/book/{book_id}/"
        return book_id, scope

    def extract_book_info(self, html: str, url: str) -> tuple[BookMeta, list[TocEntry]]:
        soup = BeautifulSoup(html, "lxml")
        book_id, scope_url = self.extract_book_scope(url)

        # 1. استخراج العنوان
        title = None
        # أ) من شريط التنقل المتدرج (#main-rtl div.b)
        breadcrumb = soup.select_one("#main-rtl > div.b, #main-rtl > h3")
        if breadcrumb:
            text = breadcrumb.get_text(" ", strip=True)
            if "»" in text:
                title = text.split("»")[-1].strip()

        # ب) من وسم og:title أو title
        if not title:
            og_title = soup.find("meta", property="og:title")
            if og_title and og_title.get("content"):
                t = og_title["content"]
                # حذف لاحقة الموقع المعتادة
                title = re.split(r"\s*-\s*موقع مكتب", t)[0].strip()

        if not title and soup.title:
            t = soup.title.get_text(strip=True)
            title = re.split(r"\s*-\s*موقع مكتب", t)[0].strip()

        if not title:
            title = f"كتاب {book_id}"

        # 2. صورة الغلاف
        cover_url = None
        cover_div = soup.select_one(".book-cover")
        if cover_div and cover_div.get("style"):
            m = re.search(r"url\((['\"]?)(.*?)\1\)", cover_div["style"])
            if m:
                cover_url = urljoin(url, m.group(2))

        if not cover_url:
            og_img = soup.find("meta", property="og:image")
            if og_img and og_img.get("content"):
                cover_url = urljoin(url, og_img["content"])

        # 3. الوصف
        desc = None
        meta_desc = soup.find("meta", attrs={"name": "description"})
        if meta_desc and meta_desc.get("content"):
            desc = meta_desc["content"].strip()

        meta = BookMeta(
            title=title,
            author="السيد علي الحسيني السيستاني",
            publisher="موقع مكتب سماحة المرجع الديني الأعلى السيد علي الحسيني السيستاني (دام ظله)",
            language="ar",
            direction="rtl",
            source_name="موقع مكتب سماحة السيد السيستاني",
            source_url=scope_url,
            cover_url=cover_url,
            description=desc,
            extracted_at=datetime.now(),
        )

        # 4. استخراج روابط الفهرس
        toc_entries: list[TocEntry] = []
        seen_urls: set[str] = set()

        content_box = soup.select_one("#main-book-content")
        if not content_box:
            raise ParseError(f"لم يتم العثور على #main-book-content في {url}")

        links = content_box.select("ul.baz > li > a")
        for a in links:
            href = a.get("href")
            if not href:
                continue
            full_url = normalize_url(href, base=url, host_aliases=_HOST_ALIASES)
            # التأكد الصارم من أن الرابط يقع داخل نطاق الكتاب
            if not full_url.startswith(scope_url):
                continue
            if full_url in seen_urls:
                continue

            seen_urls.add(full_url)
            text = a.get_text(" ", strip=True)
            # تقسيم العنوان بالـ » للحصول على الشجرة الهرمية
            parts = [p.strip() for p in text.split("»") if p.strip()]
            if not parts:
                parts = ["بدون عنوان"]

            toc_entries.append(TocEntry(url=full_url, path=parts))

        if not toc_entries:
            # ربما الكتاب صفحة وحيدة أو ملف PDF فقط (مثل بعض الكتيبات)
            pdf_link = content_box.select_one("a.book-icon.pdf, a[href$='.pdf']")
            if pdf_link:
                raise NoContentError(
                    f"هذا الكتاب متوفر كملف PDF فقط ولا يحتوي على صفحات نصية ويب: {pdf_link.get('href')}"
                )
            raise NoContentError(f"لم يتم العثور على أي صفحات نصية في فهرس هذا الكتاب: {url}")

        return meta, toc_entries

    def extract_page_content(self, html: str, url: str) -> PageContent:
        soup = BeautifulSoup(html, "lxml")

        # 1. عنوان الصفحة
        title_el = soup.select_one("#main-rtl h1.c, #main-rtl h1")
        title = title_el.get_text(" ", strip=True) if title_el else None

        # 2. متن الصفحة
        body_el = soup.select_one("#main-book-content div.book-text, #main-rtl div.book-text")
        if not body_el:
            # في حال لم يكن داخل #main-book-content
            body_el = soup.select_one("div.book-text")

        if not body_el:
            raise ParseError(f"لم يتم العثور على متن الكتاب (div.book-text) في الصفحة: {url}")

        # 3. الحواشي إن وجدت
        footnote_el = soup.select_one("#main-book-content div.book-footnote, #main-rtl div.book-footnote, div.book-footnote")

        return PageContent(title=title, body=body_el, footnotes=footnote_el)

    def extract_next_url(self, html: str, current_url: str) -> str | None:
        """استخراج رابط التالي (class='fl') إن وجد وكان داخل نطاق الكتاب."""
        _, scope_url = self.extract_book_scope(current_url)
        soup = BeautifulSoup(html, "lxml")

        # في تصميم موقع السيستاني: fl هو رابط الصفحة التالية (بسهم يسار ←)
        next_link = soup.select_one("#main-book-content a.fl")
        if next_link and next_link.get("href"):
            full_next = normalize_url(next_link["href"], base=current_url, host_aliases=_HOST_ALIASES)
            if full_next.startswith(scope_url):
                return full_next
        return None

    def extract_category_books(self, html: str, url: str) -> list[tuple[str, str]]:
        """استخراج جميع روابط الكتب من صفحة تصنيف مثل /arabic/book/fatwa/ أو /arabic/book/"""
        soup = BeautifulSoup(html, "lxml")
        results: list[tuple[str, str]] = []
        seen: set[str] = set()

        # البحث في الجدول
        for a in soup.select("#main-book-content table td.r a, #main-book-content table a"):
            href = a.get("href")
            if not href:
                continue
            full = normalize_url(href, base=url, host_aliases=_HOST_ALIASES)
            m = _BOOK_URL_PATTERN.match(urlsplit(full).path)
            if m and full not in seen:
                seen.add(full)
                title = a.get_text(" ", strip=True)
                results.append((title, full))

        # أو في القوائم المنسدلة للبحث في الكتب
        if not results:
            for opt in soup.select("select#bid4search option"):
                val = opt.get("value")
                if val and val.isdigit():
                    book_url = f"https://www.sistani.org/arabic/book/{val}/"
                    if book_url not in seen:
                        seen.add(book_url)
                        results.append((opt.get_text(" ", strip=True), book_url))

        return results
