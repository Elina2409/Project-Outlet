"""Scraper for intersport.no.

Not yet configured. Fill in START_URL, PRODUCT_LINK_PATTERN, and the
pagination strategy once verified against the live site, then remove the
NotImplementedError guard below. See count_products_by_pagination in
_common.py for the available strategies.
"""
from __future__ import annotations

from playwright.sync_api import Page


def get_sku_count(page: Page) -> int:
    raise NotImplementedError(
        "intersport scraper not yet configured - see TODO in scrapers/intersport.py"
    )
