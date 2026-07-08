"""Scraper for intersport.no.

Each category page (e.g. /klaer) renders the site's own product total as
plain body text - both "<N> PRODUKTER" next to the filter bar and
"VISER 15 AV <N> PRODUKTER" in the pagination footer (confirmed via
probe_source.py, 2026-07-08). No stable CSS selector was verified for
either element (unlike sport1's span.text-secondary), so the count is
read straight from the rendered body text rather than guessing a class
name - never guess selectors.

Category URLs are resolved from the homepage top navigation by visible
link text - never guessed. Merker/Nyheter(kolleksjon)/Kampanjer/Outlet
are deliberately excluded: brands, campaigns and outlet are
cross-cutting views, not assortment categories (same policy as xxl and
sport1).

No whole-catalog page is known for this site, so the "all" row is an
error row by design; the data lives in the per-category rows. Summing
categories would overcount (products can appear in several).
"""
from __future__ import annotations

import re
import unicodedata

from playwright.sync_api import Page

from ._common import ScrapeError, dismiss_cookie_banner

HOME_URL = "https://www.intersport.no/"

# Top-navigation product categories (as rendered in the nav, minus the
# excluded cross-cutting entries).
CATEGORY_NAMES = [
    "Klær",
    "Sko",
    "Sykkel",
    "Sport og ballspill",
    "Friluft",
    "Trening og helse",
    "Vintersport",
]

# "<count> PRODUKTER" - the count may carry thousands separators
# (regular/non-breaking/narrow space, or dot). Matches both the filter
# bar's "8707 PRODUKTER" and the pagination footer's
# "VISER 15 AV 8707 PRODUKTER" (the leading "15 AV" doesn't satisfy the
# immediate digit-then-produkter requirement, so only the real total
# matches); "LIGNENDE/RELATERTE PRODUKTER" (no leading number) never
# matches either.
_PRODUKTER_RE = re.compile(r"(\d[\d   .]*)\s*produkter\b", re.IGNORECASE)


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
        if "intersport.no" not in anchor["href"]:
            continue
        first_line = anchor["text"].splitlines()[0] if anchor["text"] else ""
        for candidate in (anchor["text"], first_line, anchor["aria"]):
            key = _norm(candidate)
            if key in wanted and wanted[key] not in resolved:
                resolved[wanted[key]] = anchor["href"]
                break
    if not resolved:
        raise ScrapeError(f"no category links found in the top navigation of {HOME_URL}")
    return resolved


def get_sku_count(page: Page, url: str | None = None) -> int:
    """Count for one category page: the site's own "<N> PRODUKTER" label.

    Called without a URL (the whole-catalog "all" row) this fails on
    purpose - intersport.no has no known all-products page, and summing
    the overlapping categories would overcount.
    """
    if url is None:
        raise ScrapeError(
            "intersport.no has no known whole-catalog page - counts are per category"
        )
    page.goto(url, wait_until="domcontentloaded")
    dismiss_cookie_banner(page)
    # The grid and filter bar render client-side; poll for the count
    # label (up to 30s).
    for _ in range(60):
        try:
            match = _PRODUKTER_RE.search(page.inner_text("body"))
        except Exception:
            match = None
        if match:
            return _parse_count(match.group(1))
        page.wait_for_timeout(500)
    raise ScrapeError(f"no '<N> PRODUKTER' label appeared within 30s at {url}")
