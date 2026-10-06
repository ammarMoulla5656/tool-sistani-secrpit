"""
scraper.py — منسق عملية الزحف والاستخراج ومعالجة صفحات الكتاب.

المسؤوليات:
- اكتشاف نطاق الكتاب عبر المحول المناسب (Adapter)
- جلب الفهرس الأصلي واستخراج قائمة الصفحات
- اكتشاف الصفحات المفقودة عبر روابط "التالي" إن وُجدت
- تنزيل ومعالجة كل صفحة وتحويلها إلى بنية Page نظيفة
- استخراج وتنزيل الصور المعتمدة فقط
- تسجيل الأخطاء دون إيقاف العملية بالكامل
- تقديم تقارير وإشارات تقدم واضحة للمستخدم
"""
from __future__ import annotations

import logging
from collections import deque
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit

from adapters.registry import get_adapter_for_url
from config import Config
from downloader import Downloader, DownloadError
from models import Book, FailedPage, ImageAsset, Page
from parser import PageParser
from urlutils import normalize_url, safe_filename

logger = logging.getLogger("book2epub.scraper")


class BookScraper:
    def __init__(self, config: Config):
        self.config = config
        self.downloader = Downloader(config)

    def scrape_book(
        self,
        book_url: str,
        progress_callback: Callable[[int, int, str], None] | None = None,
    ) -> Book:
        """استخراج كتاب كامل من الرابط المعطى."""
        adapter = get_adapter_for_url(book_url)
        book_id, scope_url = adapter.extract_book_scope(book_url)

        logger.info(f"بدء استخراج الكتاب: {book_url} (نطاق: {scope_url})")

        # 1. جلب صفحة الفهرس الرئيسية للكتاب
        main_html = self.downloader.get_html(scope_url)
        meta, toc_entries = adapter.extract_book_info(main_html, scope_url)

        # تطبيق أي تعديلات يدوية من الإعدادات
        if self.config.title:
            meta.title = self.config.title
        if self.config.author:
            meta.author = self.config.author
        if self.config.publisher:
            meta.publisher = self.config.publisher
        if self.config.language:
            meta.language = self.config.language

        # 2. تنزيل الغلاف إن وجد
        cover_asset = None
        if meta.cover_url and self.config.include_cover:
            try:
                c_data, c_mime = self.downloader.get_image(meta.cover_url)
                ext = ".jpg" if "jpeg" in c_mime else (".png" if "png" in c_mime else ".jpg")
                cover_asset = ImageAsset(
                    source_url=meta.cover_url,
                    filename=f"cover{ext}",
                    media_type=c_mime,
                    data=c_data,
                )
            except Exception as e:
                logger.warning(f"تعذر تنزيل صورة الغلاف: {e}")

        # 3. إعداد طابور الصفحات ونظام منع التكرار
        visited_urls: set[str] = set()
        queue: deque[tuple[str, list[str], bool]] = deque()

        for entry in toc_entries:
            queue.append((entry.url, entry.path, entry.discovered))

        pages: list[Page] = []
        failed_pages: list[FailedPage] = []
        seen_images: dict[str, ImageAsset] = {}

        parser = PageParser(book_scope=scope_url)

        total_discovered = len(queue)
        processed_count = 0

        # خريطة لربط روابط الصور المحلية
        def resolve_img_href(src_url: str) -> str:
            norm_src = normalize_url(src_url, trailing_slash=False)
            if norm_src not in seen_images:
                try:
                    idata, imime = self.downloader.get_image(norm_src)
                    path_name = urlsplit(norm_src).path.split("/")[-1] or "img"
                    ext = ".jpg" if "jpeg" in imime else (".png" if "png" in imime else ".bin")
                    if not any(path_name.lower().endswith(x) for x in (".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg")):
                        path_name += ext
                    fname = f"img_{len(seen_images) + 1:03d}_{safe_filename(path_name, max_len=25)}"
                    seen_images[norm_src] = ImageAsset(
                        source_url=norm_src,
                        filename=fname,
                        media_type=imime,
                        data=idata,
                    )
                except Exception as ex:
                    logger.warning(f"فشل جلب صورة من المتن {src_url}: {ex}")
                    return src_url
            return f"../Images/{seen_images[norm_src].filename}"

        # 4. المرور على الصفحات
        while queue:
            if len(visited_urls) >= self.config.max_pages:
                logger.warning(f"تم الوصول إلى الحد الأقصى للصفحات ({self.config.max_pages})")
                break

            current_url, current_path, is_discovered = queue.popleft()
            norm_url = normalize_url(current_url)

            if norm_url in visited_urls:
                continue
            visited_urls.add(norm_url)

            processed_count += 1
            page_title_preview = " » ".join(current_path) if current_path else norm_url
            if progress_callback:
                progress_callback(processed_count, total_discovered, page_title_preview)

            try:
                # أ) جلب HTML الصفحة
                page_html = self.downloader.get_html(norm_url)

                # ب) استخراج المحتوى عبر الـ Adapter
                page_content = adapter.extract_page_content(page_html, norm_url)

                # ج) تنظيف المحتوى وتحويله لكتل XHTML
                p_title, blocks, anchors, found_imgs = parser.parse_page(
                    page_content,
                    page_url=norm_url,
                    image_resolver=resolve_img_href if self.config.download_images else None,
                )

                # د) تنزيل الصور المكتشفة في المتن
                if self.config.download_images:
                    for img_url in found_imgs:
                        resolve_img_href(img_url)

                # هـ) فحص رابط "التالي" لاكتشاف أي صفحات غير موجودة في الفهرس
                if self.config.follow_next_links:
                    next_url = adapter.extract_next_url(page_html, norm_url)
                    if next_url:
                        norm_next = normalize_url(next_url)
                        if norm_next.startswith(scope_url) and norm_next not in visited_urls:
                            if not any(q[0] == norm_next for q in queue):
                                queue.append((norm_next, ["صفحة مكملة"], True))
                                total_discovered += 1

                pages.append(Page(
                    url=norm_url,
                    path=current_path,
                    blocks=blocks,
                    anchors=anchors,
                    discovered=is_discovered,
                    source_title=p_title,
                ))

            except Exception as e:
                logger.error(f"خطأ في معالجة الصفحة {norm_url}: {e}")
                failed_pages.append(FailedPage(url=norm_url, title=page_title_preview, error=str(e)))

        logger.info(f"اكتمل الاستخراج: {len(pages)} صفحة ناجحة، {len(failed_pages)} صفحات فشلت")

        return Book(
            meta=meta,
            pages=pages,
            images=list(seen_images.values()),
            cover=cover_asset,
            failed=failed_pages,
            toc_count=len(toc_entries),
        )
