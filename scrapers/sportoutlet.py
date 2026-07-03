"""Scraper for sportoutlet.no.

Counts come from the site's own categories API instead of scraping
rendered product tiles: https://sportoutlet.no/api/v1/categories returns
every main group (category) with an `articlesCount` field. The rendered
catalog is capped - pages stop loading around ~1700 tiles no matter how
far you scroll (verified live: the Klær page rendered 1732 tiles while
the API reports 4275 articles), so DOM counting silently undercounts
large categories. The API numbers are the site's own and authoritative.

Payload shape (verified in the browser Network tab): a top-level array
of main-group dicts {MainGroupID, Name, articlesCount, ArticleGroup2s:
[...subgroups with ArticleGroup2ID...], ...}. Main groups are identified
as dicts carrying a string Name and an articlesCount but no
ArticleGroup2ID, so subgroup entries are never mistaken for categories.

The whole-catalog "all" row is the sum over main groups; if the site
lists a product under two main groups it is counted twice there.
"""
from __future__ import annotations

from playwright.sync_api import Page

from ._common import ScrapeError

CATEGORIES_API_URL = "https://sportoutlet.no/api/v1/categories"

# Expected top-level categories. Counts and names are taken from the API
# response itself; this list only lets main.py flag (as error rows) any
# category the API stops returning.
CATEGORY_NAMES = [
    "Klær",
    "Sko",
    "Friluft og camping",
    "Sportsutstyr",
    "Trimutstyr",
    "Fiskeutstyr",
    "Mat og drikke",
    "Vintersport",
    "Lagsport",
    "Sykkel",
    "Kjæledyr",
]


def _main_groups(page: Page) -> list[tuple[str, int]]:
    """(name, articlesCount) per main group, straight from the API."""
    response = page.request.get(CATEGORIES_API_URL)
    if not response.ok:
        raise ScrapeError(f"categories API returned HTTP {response.status}")
    data = response.json()

    groups: list[tuple[str, int]] = []

    def walk(node) -> None:
        if isinstance(node, dict):
            if (
                isinstance(node.get("Name"), str)
                and isinstance(node.get("articlesCount"), int)
                and "ArticleGroup2ID" not in node
            ):
                groups.append((node["Name"].strip(), node["articlesCount"]))
                return  # children are subgroups, not categories
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(data)
    if not groups:
        raise ScrapeError(
            "categories API payload contained no main groups with articlesCount"
        )
    return groups


def get_sku_count(page: Page) -> int:
    """Whole-catalog count: sum of all main-group article counts."""
    return sum(count for _, count in _main_groups(page))


def get_category_counts(page: Page) -> dict[str, int]:
    return dict(_main_groups(page))
