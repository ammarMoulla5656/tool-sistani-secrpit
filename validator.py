"""
validator.py — فحص جودة وصحة ملفات EPUB عبر أداة EPUBCheck الرسمية.

المميزات:
- الكشف التلقائي عن جافا ومسار مكتبة EPUBCheck المحلية
- معالجة ذكية لترميز الحروف العربية على بيئات Windows (نسخ مؤقت بمسار ASCII أثناء الفحص)
- تحليل دقيق للمخرجات وتقديم تقرير بإحصائيات الأخطاء والتحذيرات
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from config import Config

logger = logging.getLogger("book2epub.validator")


@dataclass
class ValidationResult:
    available: bool
    is_valid: bool = False
    fatal_count: int = 0
    error_count: int = 0
    warning_count: int = 0
    info_count: int = 0
    messages: list[str] = field(default_factory=list)
    raw_output: str = ""


class EPUBValidator:
    def __init__(self, config: Config):
        self.config = config
        self.jar_path = self._find_epubcheck_jar()
        self.java_cmd = self._find_java()

    def _find_epubcheck_jar(self) -> Path | None:
        """البحث عن jar الخاص بـ EPUBCheck في مجلد tools."""
        candidate_dirs = [
            self.config.tools_dir,
            self.config.tools_dir / "epubcheck-5.4.0",
        ]
        for cdir in candidate_dirs:
            if cdir.exists():
                for p in cdir.rglob("epubcheck.jar"):
                    return p
        return None

    def _find_java(self) -> str | None:
        """التحقق من توفر أمر java في النظام."""
        cmd = shutil.which("java")
        return cmd

    def is_available(self) -> bool:
        return bool(self.java_cmd and self.jar_path and self.jar_path.exists())

    def validate(self, epub_path: Path) -> ValidationResult:
        """التحقق من ملف الـ EPUB وإرجاع النتيجة التفصيلية."""
        if not self.is_available():
            reason = "لم يتم العثور على Java أو ملف epubcheck.jar"
            logger.info(f"تخطي فحص EPUBCheck: {reason}")
            return ValidationResult(available=False, messages=[reason])

        epub_path = Path(epub_path).resolve()
        if not epub_path.exists():
            return ValidationResult(available=True, is_valid=False, error_count=1, messages=[f"الملف غير موجود: {epub_path}"])

        # معالجة ترميز أسماء الملفات على Windows:
        # بعض نسخ جافا على ويندوز تواجه مشاكل مع الحروف غير اللاتينية في وسائط الأوامر
        # لذلك نستخدم ملفًا مؤقتًا باسم لاتيني نقي لضمان دقة الفحص
        temp_file = None
        target_to_check = epub_path
        if os.name == "nt" and not epub_path.name.isascii():
            try:
                fd, temp_file = tempfile.mkstemp(suffix=".epub", prefix="chk_")
                os.close(fd)
                shutil.copyfile(epub_path, temp_file)
                target_to_check = Path(temp_file)
            except Exception as e:
                logger.debug(f"تعذر إنشاء نسخة مؤقتة للفحص: {e}")

        try:
            cmd = [
                self.java_cmd,
                "-Dfile.encoding=UTF-8",
                "-jar",
                str(self.jar_path),
                str(target_to_check),
            ]
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            raw = (proc.stdout or "") + "\n" + (proc.stderr or "")

            # تحليل المخرجات
            # مثال: Messages: 0 fatals / 0 errors / 0 warnings / 0 infos
            m = re.search(r"(\d+)\s+fatals\s*/\s*(\d+)\s+errors\s*/\s*(\d+)\s+warnings\s*/\s*(\d+)\s+infos", raw)
            if m:
                fatals = int(m.group(1))
                errors = int(m.group(2))
                warnings = int(m.group(3))
                infos = int(m.group(4))
            else:
                fatals = 1 if "FATAL" in raw else 0
                errors = 1 if proc.returncode != 0 else 0
                warnings = 0
                infos = 0

            # استخراج رسائل الخطأ والتحذير المحددة
            messages = []
            for line in raw.splitlines():
                line_s = line.strip()
                if any(k in line_s for k in ("ERROR(", "WARNING(", "FATAL(", "Check finished")):
                    messages.append(line_s)

            is_valid = (proc.returncode == 0) and (fatals == 0) and (errors == 0)

            return ValidationResult(
                available=True,
                is_valid=is_valid,
                fatal_count=fatals,
                error_count=errors,
                warning_count=warnings,
                info_count=infos,
                messages=messages,
                raw_output=raw,
            )
        except Exception as e:
            return ValidationResult(
                available=True,
                is_valid=False,
                error_count=1,
                messages=[f"استثناء أثناء تشغيل EPUBCheck: {e}"],
            )
        finally:
            if temp_file and os.path.exists(temp_file):
                try:
                    os.remove(temp_file)
                except Exception:
                    pass
