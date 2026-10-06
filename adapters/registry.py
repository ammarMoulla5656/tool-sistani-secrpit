"""
adapters/registry.py — سجل المحولات واكتشاف المحول المناسب للرابط.
"""
from __future__ import annotations

from adapters.base import SiteAdapter
from adapters.sistani import SistaniAdapter

_ADAPTERS: list[type[SiteAdapter]] = [
    SistaniAdapter,
]


def register_adapter(adapter_cls: type[SiteAdapter]) -> None:
    """تسجيل محول جديد في حال إضافة مواقع أخرى."""
    if adapter_cls not in _ADAPTERS:
        _ADAPTERS.append(adapter_cls)


def get_adapter_for_url(url: str) -> SiteAdapter:
    """إرجاع المحول المناسب للرابط المعطى."""
    for adapter_cls in _ADAPTERS:
        if adapter_cls.can_handle(url):
            return adapter_cls()
    raise ValueError(f"لا يوجد محول مدعوم لهذا الرابط: {url}")
