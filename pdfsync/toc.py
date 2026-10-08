"""pdfsync/toc.py — عقدة فهرس بسيطة مستقلة (لا تعتمد على epub_builder)."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class TocNode:
    title: str
    target_href: str | None = None            # نسبةً لمجلد OPF، مثل Text/page-0010.xhtml#id
    children: dict[str, "TocNode"] = field(default_factory=dict)
