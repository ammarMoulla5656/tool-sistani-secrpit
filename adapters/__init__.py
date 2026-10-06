"""
adapters package — محولات المواقع المختلفة لدعم استخراج الكتب بمرونة.
"""
from adapters.base import SiteAdapter
from adapters.registry import get_adapter_for_url

__all__ = ["SiteAdapter", "get_adapter_for_url"]
