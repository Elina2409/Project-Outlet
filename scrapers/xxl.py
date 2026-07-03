"""Scraper for xxl.no.

XXL's catalog data comes from the Apptus eSales storefront API
(*.api.esales.apptus.cloud, "landing-page" query). Its URL carries
per-visitor customerKey/sessionKey values, so the API is never called
directly; instead we load a category page in the browser and intercept
the site's own API responses. The navigation tree in those responses
holds an article count per node: {path: "/", label: "All", count: N} is
the site-wide total (verified live: 23246), and the current category's
node carries its own count (e.g. label "Dame" -> 4228, matching the
rendered "Artikler: 4228" label).

Counts are read from the intercepted API first; the rendered
"Artikler: <N>" text is only a fallback (in headless runs it often never
renders - a live run had every category time out waiting for it even
though the API responses were flowing).

Category URLs (e.g. /dame/c/9200000) are resolved from the homepage top
navigation by visible link text - never guessed.
"""
from __future__ import annotations

import re
import unicodedata

from playwright.sync_api import Page

from ._common import ScrapeError, dismiss_cookie_banner

HOME_URL = "https://www.xxl.no/"

# Top-navigation product categories. Kampanjer/Varemerker (campaigns,
# brands) and Outlet (a price status, not an assortment category) are
# deliberately excluded.
CATEGORY_NAMES = [
    "Dame",
    "Herre",
    "Barn",
    "Sykkel",
    "Sport",
    "Løping",
    "Turutstyr",
    "Jakt & Fiske",
    "Vannsport",
]

_ARTICLES_RE = re.compile(r"Artikler:\s*([\d][\d  .]*)")


def _norm(text: str) -> str:
    return unicodedata.normalize("NFC", text).strip().casefold()


def _find_labeled_count(node, label: str, path: str | None = None) -> int | None:
    """Find the count of the JSON node with the given label. Nodes marked
    selected win (that's the category the page is currently showing);
    label-only matches without a count (e.g. breadcrumbs) are ignored.

    When `path` is given, only a node with that exact path qualifies -
    category pages also carry category-scoped "All" facet nodes, so the
    site-wide total must be pinned to the root node {path: "/"}."""
    matches: list[dict] = []

    def walk(item) -> None:
        if isinstance(item, dict):
            if item.get("label") == label and isinstance(item.get("count"), int):
                matches.append(item)
            for value in item.values():
                walk(value)
        elif isinstance(item, list):
            for value in item:
                walk(value)

    walk(node)
    if path is not None:
        for match in matches:
            if match.get("path") == path:
                return match["count"]
        return None
    for match in matches:
        if match.get("selected"):
            return match["count"]
    return matches[0]["count"] if matches else None


def get_categories(page: Page) -> dict[str, str]:
    """Category name -> URL from the homepage top navigation."""
    wanted = {_norm(name): name for name in CATEGORY_NAMES}
    page.goto(HOME_URL, wait_until="domcontentloaded")
    dismiss_cookie_banner(page)
    page.wait_for_timeout(2000)
    anchors = page.eval_on_selector_all(
        "a[href]", "els => els.map(e => ({text: e.innerText.trim(), href: e.href}))"
    )
    resolved: dict[str, str] = {}
    for anchor in anchors:
        key = _norm(anchor["text"])
        if key in wanted and wanted[key] not in resolved and "/c/" in anchor["href"]:
            resolved[wanted[key]] = anchor["href"]
    if not resolved:
        raise ScrapeError(f"no category links found in the top navigation of {HOME_URL}")
    return resolved


def _count_from_page(
    page: Page,
    url: str,
    label: str,
    timeout_s: int = 30,
    path: str | None = None,
    allow_label_fallback: bool = True,
) -> int:
    """Load `url`, intercept the site's eSales API responses, and return
    the count of the tree node with `label` (and `path`, if given). Falls
    back to the rendered "Artikler: <N>" text if the API never yields the
    node - unless disabled: on a category page that label shows the
    category's count, so it must never stand in for the site-wide total."""
    payloads: list = []

    def on_response(response) -> None:
        if "apptus.cloud" not in response.url:
            return
        try:
            payloads.append(response.json())
        except Exception:
            pass

    page.on("response", on_response)
    try:
        page.goto(url, wait_until="domcontentloaded")
        dismiss_cookie_banner(page)
        for _ in range(timeout_s * 2):
            for data in payloads:
                count = _find_labeled_count(data, label, path=path)
                if count is not None:
                    return count
            if allow_label_fallback:
                try:
                    match = _ARTICLES_RE.search(page.inner_text("body"))
                except Exception:
                    match = None
                if match:
                    return int(re.sub(r"\D", "", match.group(1)))
            page.wait_for_timeout(500)
    finally:
        page.remove_listener("response", on_response)

    raise ScrapeError(
        f"no count for {label!r} in {len(payloads)} API responses and no "
        f"'Artikler' label rendered at {url}"
    )


def get_sku_count(page: Page) -> int:
    """Site-wide total: the navigation tree's "All" node, intercepted on
    the first category page (the PLP query doesn't fire on the homepage)."""
    categories = get_categories(page)
    first_url = next(iter(categories.values()))
    return _count_from_page(page, first_url, "All", path="/", allow_label_fallback=False)


def get_category_counts(page: Page) -> dict[str, int]:
    """Per-category counts from each category page's own API responses.
    Failed categories are omitted; main.py reports them as error rows."""
    counts: dict[str, int] = {}
    for name, url in get_categories(page).items():
        try:
            counts[name] = _count_from_page(page, url, name)
        except Exception:
            continue
    return counts
