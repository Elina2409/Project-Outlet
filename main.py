"""Entry point: run every site scraper once, write results, print summary.

Usage: python main.py [--workers N]
No scheduler involved - run this whenever you want a fresh snapshot.

For each site, records one "all" row (whole catalog). Sites whose scraper
module exports get_categories() additionally get one row per category.
Note that per-category counts can sum to more than the "all" count when a
product is listed in several categories.

Counts run in a thread pool (default 4 workers); each worker drives its
own headless browser, since Playwright's sync API must not be shared
across threads. Phase 1 handles whole-catalog counts + category
discovery per site, phase 2 fans the discovered category pages out
across workers.
"""
from __future__ import annotations

import argparse
import sys
from concurrent.futures import ThreadPoolExecutor

from scrapers import SITES
from scrapers._common import SiteResult, browser_page, write_result

DEFAULT_WORKERS = 4


def _err_note(exc: Exception) -> str:
    """Exception message flattened to one line, truncated for the CSV."""
    return " ".join(str(exc).split())[:200]


CategoryTask = tuple[str, object, str, str]  # site, module, category, url


def run_site(name: str, module) -> tuple[list[SiteResult], list[CategoryTask]]:
    """Whole-catalog count + category discovery for one site."""
    results: list[SiteResult] = []
    tasks: list[CategoryTask] = []
    try:
        with browser_page() as page:
            try:
                count = module.get_sku_count(page)
                results.append(SiteResult(site=name, sku_count=count, status="ok"))
            except Exception as exc:
                results.append(
                    SiteResult(site=name, sku_count=None, status="error", note=_err_note(exc))
                )

            if hasattr(module, "get_category_counts"):
                # Site exposes per-category counts directly (e.g. from its
                # own API) - no per-category page visits needed.
                try:
                    category_counts = module.get_category_counts(page)
                except Exception as exc:
                    category_counts = {}
                    results.append(
                        SiteResult(
                            site=name,
                            sku_count=None,
                            status="error",
                            note=f"category counts failed: {_err_note(exc)}"[:200],
                            category="(categories)",
                        )
                    )
                for cat_name, count in category_counts.items():
                    results.append(
                        SiteResult(site=name, sku_count=count, status="ok", category=cat_name)
                    )
                for missing in getattr(module, "CATEGORY_NAMES", []):
                    if missing not in category_counts:
                        results.append(
                            SiteResult(
                                site=name,
                                sku_count=None,
                                status="error",
                                note="category missing from source",
                                category=missing,
                            )
                        )
            elif hasattr(module, "get_categories"):
                try:
                    categories = module.get_categories(page)
                except Exception as exc:
                    categories = {}
                    results.append(
                        SiteResult(
                            site=name,
                            sku_count=None,
                            status="error",
                            note=f"category discovery failed: {_err_note(exc)}"[:200],
                            category="(categories)",
                        )
                    )
                # Categories the module knows about but couldn't resolve a
                # URL for must surface as error rows, not vanish silently.
                for missing in getattr(module, "CATEGORY_NAMES", []):
                    if missing not in categories:
                        results.append(
                            SiteResult(
                                site=name,
                                sku_count=None,
                                status="error",
                                note="no URL resolved for this category",
                                category=missing,
                            )
                        )
                for cat_name, cat_url in categories.items():
                    tasks.append((name, module, cat_name, cat_url))
    except Exception as exc:  # isolate failures - one site must never stop the others
        results.append(
            SiteResult(site=name, sku_count=None, status="error", note=_err_note(exc))
        )
    return results, tasks


def run_category(task: CategoryTask) -> SiteResult:
    """Count one category page in its own browser."""
    site, module, cat_name, cat_url = task
    try:
        with browser_page() as page:
            count = module.get_sku_count(page, cat_url)
        return SiteResult(site=site, sku_count=count, status="ok", category=cat_name)
    except Exception as exc:
        return SiteResult(
            site=site, sku_count=None, status="error", note=_err_note(exc), category=cat_name
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--workers",
        type=int,
        default=DEFAULT_WORKERS,
        help=f"parallel browser workers (default {DEFAULT_WORKERS})",
    )
    args = parser.parse_args()

    results: list[SiteResult] = []
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        site_outcomes = list(pool.map(lambda s: run_site(*s), SITES))
    category_tasks: list[CategoryTask] = []
    for site_results, tasks in site_outcomes:
        results.extend(site_results)
        category_tasks.extend(tasks)
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        results.extend(pool.map(run_category, category_tasks))

    for result in results:
        write_result(result)

    print(f"{'Site':<15}{'Category':<22}{'SKUs':>8}  Status")
    print("-" * 65)
    for r in sorted(results, key=lambda r: (r.site, r.category != "all", r.category)):
        count_display = r.sku_count if r.sku_count is not None else "NULL"
        note = f" - {r.note}" if r.note else ""
        print(f"{r.site:<15}{r.category:<22}{count_display!s:>8}  {r.status}{note}")

    # Every single row failing means the run produced no data at all -
    # exit nonzero so CI marks the run red.
    if results and all(r.status == "error" for r in results):
        sys.exit(1)


if __name__ == "__main__":
    main()
