"""
downloader.py — محرك التنزيل المحترم وإدارة الـ Cache.

المميزات:
- إدارة كاش ثنائي: cache/html و cache/images
- التزام بقواعد robots.txt
- معدل طلبات هادئ (delay + jitter)
- إعادة محاولة مع Exponential Backoff للأخطاء المؤقتة (5xx, 429, timeouts)
- User-Agent واضح يوضح غرض الأرشفة الشخصية
- وضع عدم الاتصال (--offline) لإعادة البناء السريعة دون اتصال بالإنترنت
"""
from __future__ import annotations

import hashlib
import logging
import random
import time
from pathlib import Path
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

import requests

from config import Config
from urlutils import normalize_url

logger = logging.getLogger("book2epub.downloader")


class DownloadError(Exception):
    """خطأ تنزيل دائم أو استنفاد المحاولات."""


class Downloader:
    def __init__(self, config: Config):
        self.config = config
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": config.user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/*;q=0.8,*/*;q=0.7",
            "Accept-Language": "ar,en;q=0.8",
        })

        # تهيئة مجلدات الكاش
        self.html_cache_dir = config.cache_dir / "html"
        self.img_cache_dir = config.cache_dir / "images"
        if config.use_cache:
            self.html_cache_dir.mkdir(parents=True, exist_ok=True)
            self.img_cache_dir.mkdir(parents=True, exist_ok=True)

        self._last_request_time: float = 0.0
        self._robot_parsers: dict[str, RobotFileParser] = {}

    def _get_cache_path(self, url: str, is_image: bool = False) -> Path:
        """توليد مسار كاش فريد بناءً على تجزئة SHA-256 للرابط المطبّع."""
        norm = normalize_url(url)
        h = hashlib.sha256(norm.encode("utf-8")).hexdigest()
        folder = self.img_cache_dir if is_image else self.html_cache_dir
        ext = ".bin" if is_image else ".html"
        return folder / f"{h}{ext}"

    def _check_robots(self, url: str) -> bool:
        """التحقق من ملف robots.txt للموقع."""
        if not self.config.respect_robots:
            return True

        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        if origin not in self._robot_parsers:
            rp = RobotFileParser()
            robots_url = f"{origin}/robots.txt"
            try:
                r = self.session.get(robots_url, timeout=self.config.connect_timeout)
                if r.status_code == 200:
                    rp.parse(r.text.splitlines())
                else:
                    rp.allow_all = True
            except Exception as e:
                logger.warning(f"تعذر جلب robots.txt من {origin}: {e} (السماح افتراضيًا)")
                rp.allow_all = True
            self._robot_parsers[origin] = rp

        rp = self._robot_parsers[origin]
        allowed = rp.can_fetch(self.config.user_agent, url)
        if not allowed:
            logger.warning(f"robots.txt يمنع الوصول إلى: {url}")
        return allowed

    def _rate_limit(self) -> None:
        """تطبيق مهلة هادئة بين الطلبات لاحترام الموقع."""
        if self._last_request_time == 0.0:
            return
        elapsed = time.time() - self._last_request_time
        target = self.config.delay + (random.uniform(0, self.config.jitter) if self.config.jitter > 0 else 0)
        if elapsed < target:
            time.sleep(target - elapsed)

    def get_html(self, url: str) -> str:
        """جلب محتوى صفحة HTML كنص UTF-8 مع دعم الكاش."""
        url = normalize_url(url)
        cache_file = self._get_cache_path(url, is_image=False)

        # 1. القراءة من الكاش إذا كان مفعلاً وغير مطلوب التحديث
        if self.config.use_cache and not self.config.refresh:
            if cache_file.exists():
                logger.debug(f"استرجاع من الكاش: {url}")
                return cache_file.read_text(encoding="utf-8", errors="replace")

        # 2. إذا كنا في وضع الأوفلاين ولم نجد الملف في الكاش
        if self.config.offline:
            raise DownloadError(f"الوضع بدون اتصال مفعل والصفحة غير موجودة في الكاش: {url}")

        # 3. التحقق من robots.txt
        if not self._check_robots(url):
            raise DownloadError(f"الرابط محظور بملف robots.txt: {url}")

        # 4. التنزيل مع محاولات إعادة
        last_error = None
        for attempt in range(1, self.config.retries + 1):
            try:
                self._rate_limit()
                self._last_request_time = time.time()
                resp = self.session.get(
                    url,
                    timeout=(self.config.connect_timeout, self.config.timeout),
                    allow_redirects=True,
                )
                if resp.status_code == 200:
                    resp.encoding = "utf-8"  # فرض UTF-8 لأن الموقع عربي بترميز UTF-8
                    text = resp.text

                    if self.config.use_cache:
                        cache_file.write_text(text, encoding="utf-8")
                    return text

                if resp.status_code in (404, 410):
                    raise DownloadError(f"الصفحة غير موجودة ({resp.status_code}): {url}")

                logger.warning(f"استجابة HTTP {resp.status_code} لـ {url} (المحاولة {attempt}/{self.config.retries})")
                last_error = DownloadError(f"HTTP {resp.status_code}: {url}")
            except (requests.Timeout, requests.ConnectionError) as e:
                logger.warning(f"خطأ اتصال لـ {url}: {e} (المحاولة {attempt}/{self.config.retries})")
                last_error = e

            if attempt < self.config.retries:
                sleep_sec = self.config.delay * (self.config.backoff ** (attempt - 1))
                time.sleep(sleep_sec)

        raise DownloadError(f"فشل تنزيل {url} بعد {self.config.retries} محاولات: {last_error}")

    def get_image(self, url: str) -> tuple[bytes, str]:
        """جلب بيانات الصورة ونوع الوسائط (MIME Type) مع دعم الكاش."""
        url = normalize_url(url, trailing_slash=False)
        cache_data_file = self._get_cache_path(url, is_image=True)
        cache_meta_file = cache_data_file.with_suffix(".mime")

        if self.config.use_cache and not self.config.refresh:
            if cache_data_file.exists() and cache_meta_file.exists():
                data = cache_data_file.read_bytes()
                mime = cache_meta_file.read_text(encoding="utf-8").strip()
                return data, mime

        if self.config.offline:
            raise DownloadError(f"الصورة غير متوفرة في الكاش بوضع الأوفلاين: {url}")

        last_error = None
        for attempt in range(1, self.config.retries + 1):
            try:
                self._rate_limit()
                self._last_request_time = time.time()
                resp = self.session.get(
                    url,
                    timeout=(self.config.connect_timeout, self.config.timeout),
                )
                if resp.status_code == 200:
                    data = resp.content
                    mime = resp.headers.get("Content-Type", "").split(";")[0].strip() or "image/jpeg"

                    if self.config.use_cache:
                        cache_data_file.write_bytes(data)
                        cache_meta_file.write_text(mime, encoding="utf-8")
                    return data, mime

                if resp.status_code in (404, 410):
                    raise DownloadError(f"الصورة غير موجودة ({resp.status_code}): {url}")

                last_error = DownloadError(f"HTTP {resp.status_code}: {url}")
            except Exception as e:
                last_error = e

            if attempt < self.config.retries:
                time.sleep(self.config.delay * (self.config.backoff ** (attempt - 1)))

        raise DownloadError(f"فشل تنزيل الصورة {url}: {last_error}")
