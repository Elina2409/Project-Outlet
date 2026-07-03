"""Scraper for sport1.no.

Each category page (e.g. /klaer) renders its own product total above the
grid as `<span class="text-secondary">8428</span> produkter` (element
confirmed from a live DevTools capture, 2026-07-03). That label is the
site's own number, so it is read directly - no tile counting or
scrolling, which caps out on large catalogs (see CLAUDE.md gotchas).

Category URLs are resolved from the homepage top navigation by visible
link text - never guessed. Nyheter/Merker/Kampanjer/Outlet are
deliberately excluded: news, brands, campaigns and outlet are
cross-cutting views, not assortment categories (same policy as xxl).

No whole-catalog page is known for this site, so the "all" row is an
error row by design; the data lives in the per-category rows. Summing
categories would overcount (products can appear in several).
"""
from __future__ import annotations

import re
import unicodedata

from playwright.sync_api import Page

from ._common import ScrapeError, dismiss_cookie_banner

HOME_URL = "https://www.sport1.no/"

# Top-navigation product categories (as rendered in the nav, minus the
# excluded cross-cutting entries).
CATEGORY_NAMES = [
    "Klær",
    "Sko",
    "Sykkel",
    "Friluft",
    "Sport & Ballspill",
    "Vintersport",
    "Trening & Helse",
]

# Categories the homepage nav doesn't expose as plain text-labeled
# anchors (both text and aria-label matching failed on live runs).
# URLs confirmed live by the user, 2026-07-03 - note the "-and-" slug,
# which could never be derived from the nav text. Used only for
# categories the nav scan leaves unresolved.
FALLBACK_URLS = {
    "Sykkel": "https://www.sport1.no/sykkel",
    "Trening & Helse": "https://www.sport1.no/trening-and-helse",
}

# "<count> produkter" - the count may carry thousands separators
# (regular/non-breaking/narrow space, or dot).
_PRODUKTER_RE = re.compile(r"(\d[\d\u00a0\u202f .]*)\s*produkter\b", re.IGNORECASE)


def _norm(text: str) -> str:
    """Casefold + collapse whitespace, so 'KLÆR' matches 'Klær'."""
    return " ".join(unicodedata.normalize("NFC", text).split()).casefold()


def _parse_count(text: str) -> int:
    return int(re.sub(r"\D", "", text))


def get_categories(page: Page) -> dict[str, str]:
    """Category name -> URL from the homepage top navigation."""
    wanted = {_norm(name): name for name in CATEGORY_NAMES}
    page.goto(HOME_URL, wait_until="domcontentloaded")
    dismiss_cookie_banner(page)
    page.wait_for_timeout(2000)
    anchors = page.eval_on_selector_all(
        "a[href]",
        "els => els.map(e => ({text: e.innerText.trim(), "
        "aria: e.getAttribute('aria-label') || '', href: e.href}))",
    )
    resolved: dict[str, str] = {}
    for anchor in anchors:
        if "sport1.no" not in anchor["href"]:
            continue
        # A nav anchor that wraps a dropdown menu carries the submenu
        # text in innerText too (that broke Sykkel and Trening & Helse
        # on the first live run) - the label itself is the first line.
        first_line = anchor["text"].splitlines()[0] if anchor["text"] else ""
        for candidate in (anchor["text"], first_line, anchor["aria"]):
            key = _norm(candidate)
            if key in wanted and wanted[key] not in resolved:
                resolved[wanted[key]] = anchor["href"]
                break
    for name, url in FALLBACK_URLS.items():
        resolved.setdefault(name, url)
    if not resolved:
        raise ScrapeError(f"no category links found in the top navigation of {HOME_URL}")
    return resolved


def get_sku_count(page: Page, url: str | None = None) -> int:
    """Count for one category page: the site's own "<N> produkter" label.

    Called without a URL (the whole-catalog "all" row) this fails on
    purpose - sport1.no has no known all-products page, and summing the
    overlapping categories would overcount.
    """
    if url is None:
        raise ScrapeError(
            "sport1.no has no known whole-catalog page - counts are per category"
        )
    page.goto(url, wait_until="domcontentloaded")
    dismiss_cookie_banner(page)
    # The grid renders client-side; poll for the count label (up to 30s).
    for _ in range(60):
        # Primary: the dedicated count element next to the grid header,
        # matched via its parent's text ("8428 produkter") so a stray
        # text-secondary span elsewhere can't be mistaken for it.
        parent_texts = page.eval_on_selector_all(
            "span.text-secondary",
            "els => els.map(e => e.parentElement ? e.parentElement.innerText : '')",
        )
        for text in parent_texts:
            match = _PRODUKTER_RE.search(text)
            if match:
                return _parse_count(match.group(1))
        page.wait_for_timeout(500)
    # Fallback if the class name changed: the same "<N> produkter" text
    # anywhere on the page (like xxl's rendered-label fallback).
    try:
        match = _PRODUKTER_RE.search(page.inner_text("body"))
    except Exception:
        match = None
    if match:
        return _parse_count(match.group(1))
    raise ScrapeError(f"no '<N> produkter' label appeared within 30s at {url}")
