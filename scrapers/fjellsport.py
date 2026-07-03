"""Scraper for fjellsport.no.

The homepage embeds the whole category tree - with a productCount per
node - in a script blob in the raw page source (verified via
probe_source.py on Cloud Run, 2026-07-03; e.g. `"category":{"url":
"/herreklaer","name":"Herre","productCount":4364,...}` with the same
url/name/productCount key order on every node, subcategories included).
Counts are read straight from that embedded JSON via a plain HTTP fetch
of the homepage - no page rendering or navigation at all.

The six top-level categories are pinned by URL path, verified from the
rendered top nav: Herre /herreklaer, Dame /dameklaer, Utstyr /turutstyr,
Sko og støvler /fottoy, Barn /barn, Aktiviteter /aktiviteter.
SALG/Nyheter/Fjellsportpris/Outlet/Varemerker are cross-cutting views,
not assortment categories, and are deliberately excluded.

No whole-catalog page or site-total field is known, so the "all" row is
an error row by design; summing categories would overcount.
"""
from __future__ import annotations

import re

from playwright.sync_api import Page

from ._common import ScrapeError

HOME_URL = "https://www.fjellsport.no/"

# Nav display name -> URL path of that category's node in the embedded
# tree. Paths verified live; the display names are the nav labels.
CATEGORY_PATHS = {
    "Herre": "/herreklaer",
    "Dame": "/dameklaer",
    "Utstyr": "/turutstyr",
    "Sko og støvler": "/fottoy",
    "Barn": "/barn",
    "Aktiviteter": "/aktiviteter",
}
CATEGORY_NAMES = list(CATEGORY_PATHS)

# One category node in the embedded tree. Key order verified stable
# across main and subcategory nodes.
_CATEGORY_NODE_RE = re.compile(
    r'"url":"(?P<url>/[^"]*)","name":"(?P<name>[^"]*)","productCount":(?P<count>\d+)'
)


def get_sku_count(page: Page) -> int:
    raise ScrapeError(
        "fjellsport.no has no known whole-catalog page - counts are per category"
    )


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
