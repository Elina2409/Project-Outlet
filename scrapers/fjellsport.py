"""Scraper for fjellsport.no.

The homepage embeds the whole category tree - with a productCount per
node - in a script blob in the raw page source (verified via
probe_source.py on Cloud Run, 2026-07-03; e.g. `"category":{"url":
"/herreklaer","name":"Herre","productCount":4364,...}` with the same
url/name/productCount key order on every node, subcategories included).
Counts are read straight from that embedded JSON via a plain HTTP fetch
of the homepage - no page rendering or navigation at all.

The five top-level assortment categories are pinned by URL path,
verified from the rendered top nav: Herre /herreklaer, Dame /dameklaer,
Utstyr /turutstyr, Sko og støvler /fottoy, Barn /barn.
SALG/Nyheter/Fjellsportpris/Outlet/Varemerker/Aktiviteter are
cross-cutting views, not assortment categories, and are deliberately
excluded (Aktiviteter counted ~88% of the summed catalog when checked).

The "all" row is a genuinely deduplicated total: the count of unique
canonical product pages across the sitemap files (index at
/api/sitemap/nb-no/sitemapindex.xml, named by robots.txt). A full URL
census (2026-07-03) showed product pages live at
/merker/<brand>/<product-slug> - 22205 of 23513 sitemap URLs sit under
/merker/, with brand listing pages at depth 2 and products at depth 3+;
all other prefixes are listing/content pages in the hundreds. Sitemaps
list each page once, so this number has no category overlap in it -
but product slugs can encode color variants, so it may run higher than
the catalog tree's per-product counts.
"""
from __future__ import annotations

import re

from playwright.sync_api import Page

from ._common import ScrapeError

HOME_URL = "https://www.fjellsport.no/"
SITEMAP_INDEX_URL = "https://www.fjellsport.no/api/sitemap/nb-no/sitemapindex.xml"

# <loc> only - deliberately does not match namespaced tags like
# <image:loc>.
_SITEMAP_LOC_RE = re.compile(r"<loc>([^<]+)</loc>")

# Nav display name -> URL path of that category's node in the embedded
# tree. Paths verified live; the display names are the nav labels.
# Aktiviteter (/aktiviteter) is excluded despite being in the nav: its
# count came back as 15727 on 2026-07-03 vs ~17940 summed over the five
# assortment trees, i.e. it re-groups nearly the whole catalog by
# activity - a cross-cutting view like SALG/Outlet, not an assortment.
CATEGORY_PATHS = {
    "Herre": "/herreklaer",
    "Dame": "/dameklaer",
    "Utstyr": "/turutstyr",
    "Sko og støvler": "/fottoy",
    "Barn": "/barn",
}
CATEGORY_NAMES = list(CATEGORY_PATHS)

# One category node in the embedded tree. Key order verified stable
# across main and subcategory nodes.
_CATEGORY_NODE_RE = re.compile(
    r'"url":"(?P<url>/[^"]*)","name":"(?P<name>[^"]*)","productCount":(?P<count>\d+)'
)


def get_sku_count(page: Page) -> int:
    """Deduplicated whole-catalog count: unique canonical product pages
    (/merker/<brand>/<product-slug>, i.e. depth >= 3 under /merker/)
    across the sitemap files listed by the sitemap index."""
    index = page.request.get(SITEMAP_INDEX_URL).text()
    sitemap_urls = _SITEMAP_LOC_RE.findall(index)
    if not sitemap_urls:
        raise ScrapeError(f"no sitemap files listed at {SITEMAP_INDEX_URL}")
    products: set[str] = set()
    for sitemap_url in sitemap_urls:
        xml = page.request.get(sitemap_url).text()
        for loc in _SITEMAP_LOC_RE.findall(xml):
            path = loc.split("?")[0].split("://", 1)[-1]
            parts = [p for p in path.split("/") if p][1:]  # drop the host
            if len(parts) >= 3 and parts[0] == "merker":
                products.add("/".join(parts))
    if not products:
        raise ScrapeError(
            f"no /merker/<brand>/<product> URLs found across "
            f"{len(sitemap_urls)} sitemap files"
        )
    return len(products)


def get_category_counts(page: Page) -> dict[str, int]:
    """Category name -> productCount from the homepage's embedded tree."""
    raw = page.request.get(HOME_URL).text()
    by_path: dict[str, int] = {}
    for match in _CATEGORY_NODE_RE.finditer(raw):
        # The tree can repeat a node (menus render it twice); first wins.
        by_path.setdefault(match["url"], int(match["count"]))
    counts = {
        name: by_path[path]
        for name, path in CATEGORY_PATHS.items()
        if path in by_path
    }
    if not counts:
        raise ScrapeError(
            f"no url/name/productCount nodes found in the source of {HOME_URL} "
            f"({len(raw)} chars fetched)"
        )
    return counts
