"""Shared helpers for SKU-count scrapers."""
from __future__ import annotations

import csv
import re
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterator, Optional

from playwright.sync_api import Browser, Page, sync_playwright

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
RESULTS_CSV = DATA_DIR / "sku_counts.csv"
CSV_FIELDS = ["timestamp_utc", "site", "category", "sku_count", "status", "note"]

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


@dataclass
class SiteResult:
    site: str
    sku_count: Optional[int]
    status: str  # "ok" | "error"
    note: str = ""
    category: str = "all"  # "all" = whole catalog; otherwise a category name


class ScrapeError(RuntimeError):
    """Raised when a scraper cannot produce a trustworthy count."""


@contextmanager
def browser_page() -> Iterator[Page]:
    """Launch a single headless browser page, closed automatically."""
    with sync_playwright() as pw:
        browser: Browser = pw.chromium.launch(headless=True)
        try:
            page = browser.new_page(user_agent=USER_AGENT, locale="nb-NO")
            yield page
        finally:
            browser.close()


# Button texts used by the consent banners common on Norwegian sites
# (Cookiebot, CookieInformation, custom). Matched case-insensitively.
_CONSENT_BUTTON_TEXTS = ["godta alle", "aksepter alle", "godta", "aksepter", "accept all", "tillat alle"]


def dismiss_cookie_banner(page: Page) -> None:
    """Best-effort click on a cookie-consent accept button; never raises."""
    for text in _CONSENT_BUTTON_TEXTS:
        try:
            button = page.get_by_role("button", name=re.compile(text, re.IGNORECASE)).first
            if button.is_visible(timeout=1000):
                button.click(timeout=2000)
                page.wait_for_timeout(500)
                return
        except Exception:
            continue


def count_products_by_pagination(
    page: Page,
    start_url: str,
    product_link_pattern: str,
    next_page_url: Optional[Callable[[str, int], str]] = None,
    load_more_selector: Optional[str] = None,
    max_pages: int = 500,
    stall_limit: int = 2,
) -> int:
    """Generic SKU counter: collect unique product-detail links across a
    paginated catalog until no new products appear.

    Pass exactly one pagination strategy:
    - `next_page_url(base_url, page_number) -> url` for URL-param pagination
      (e.g. `?page=N`).
    - `load_more_selector` for a "Load more" / infinite-scroll button that
      gets clicked repeatedly.
    Pass neither to scan only `start_url` as a single page.
    """
    link_re = re.compile(product_link_pattern)
    seen: set[str] = set()

    def collect_from_current_page() -> None:
        hrefs = page.eval_on_selector_all("a[href]", "els => els.map(e => e.href)")
        for href in hrefs:
            if link_re.search(href):
                seen.add(href)

    if load_more_selector:
        page.goto(start_url, wait_until="domcontentloaded")
        page.wait_for_timeout(500)
        stalled = 0
        for _ in range(max_pages):
            before = len(seen)
            collect_from_current_page()
            button = page.query_selector(load_more_selector)
            if button is None or not button.is_visible():
                break
            button.click()
            page.wait_for_timeout(800)
            stalled = stalled + 1 if len(seen) == before else 0
            if stalled >= stall_limit:
                break
    elif next_page_url:
        stalled = 0
        for i in range(1, max_pages + 1):
            page.goto(next_page_url(start_url, i), wait_until="domcontentloaded")
            page.wait_for_timeout(500)
            before = len(seen)
            collect_from_current_page()
            stalled = stalled + 1 if len(seen) == before else 0
            if stalled >= stall_limit:
                break
    else:
        page.goto(start_url, wait_until="domcontentloaded")
        page.wait_for_timeout(500)
        collect_from_current_page()

    return len(seen)


def count_unique_elements_by_scroll(
    page: Page,
    start_url: str,
    selector: str,
    attribute: str = "src",
    key_pattern: Optional[str] = None,
    fallback_attribute: Optional[str] = None,
    max_scrolls: int = 300,
    stall_limit: int = 3,
    scroll_wait_ms: int = 700,
) -> int:
    """SKU counter for infinite-scroll catalogs (no URL pagination, no
    "load more" button): repeatedly scroll to the bottom of the page,
    collecting unique product-tile keys until scrolling further adds no
    new ones.

    `selector` targets one element per product tile (e.g. a product image).
    `attribute` is read off each matched element (e.g. "src" or "alt").
    `key_pattern`, if given, is applied via re.search and the whole match
    is used as the dedupe key (e.g. to pull a stable product/article id out
    of a CDN image URL that also contains a cache-busting size suffix);
    otherwise the raw attribute value is used as-is.
    `fallback_attribute` keys tiles whose primary attribute is missing or
    doesn't match `key_pattern` (e.g. a product whose image failed to load
    and shows a no-image placeholder); without it such tiles are dropped
    from the count entirely.
    """
    key_re = re.compile(key_pattern) if key_pattern else None
    seen: set[str] = set()

    fallback_js = f"e.getAttribute('{fallback_attribute}')" if fallback_attribute else "null"
    collect_js = f"els => els.map(e => [e.getAttribute('{attribute}'), {fallback_js}])"

    def collect() -> None:
        for value, fallback in page.eval_on_selector_all(selector, collect_js):
            if value and key_re:
                match = key_re.search(value)
                if match:
                    seen.add(match.group(0))
                    continue
            elif value:
                seen.add(value)
                continue
            if fallback:
                seen.add(f"fallback:{fallback}")

    page.goto(start_url, wait_until="domcontentloaded")
    dismiss_cookie_banner(page)
    # Catalogs on these sites render client-side, so the selector may take
    # a while to appear after domcontentloaded.
    try:
        page.wait_for_selector(selector, timeout=30_000)
    except Exception:
        raise ScrapeError(
            f"no elements matching {selector!r} appeared within 30s at {page.url}"
        )
    page.wait_for_timeout(scroll_wait_ms)
    collect()

    stalled = 0
    for _ in range(max_scrolls):
        before = len(seen)
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        page.wait_for_timeout(scroll_wait_ms)
        collect()
        stalled = stalled + 1 if len(seen) == before else 0
        if stalled >= stall_limit:
            break

    if not seen:
        raise ScrapeError(
            f"elements matched {selector!r} but none yielded a product key at {page.url}"
        )
    return len(seen)


def _migrate_csv_if_needed() -> None:
    """Rewrite an existing results CSV whose header predates the current
    CSV_FIELDS (e.g. rows written before the `category` column existed);
    missing columns are filled with defaults."""
    with RESULTS_CSV.open(newline="", encoding="utf-8") as f:
        reader = csv.reader(f)
        header = next(reader, None)
    if header is None or header == CSV_FIELDS:
        return
    with RESULTS_CSV.open(newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    with RESULTS_CSV.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    "timestamp_utc": row.get("timestamp_utc", ""),
                    "site": row.get("site", ""),
                    "category": row.get("category", "all"),
                    "sku_count": row.get("sku_count", ""),
                    "status": row.get("status", ""),
                    "note": row.get("note", ""),
                }
            )


def write_result(result: SiteResult) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    is_new = not RESULTS_CSV.exists()
    if not is_new:
        _migrate_csv_if_needed()
    with RESULTS_CSV.open("a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        if is_new:
            writer.writeheader()
        writer.writerow(
            {
                "timestamp_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "site": result.site,
                "category": result.category,
                "sku_count": result.sku_count if result.sku_count is not None else "",
                "status": result.status,
                "note": result.note,
            }
        )
