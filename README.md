# Nordic Sport Retail SKU Tracker

Counts the number of products (SKUs) listed on Norwegian sport retail
websites (Sport Outlet, XXL, Anton Sport, Intersport, Sport1), whole
catalog and per category. On-demand — run locally, or via the
**Scrape SKU counts** GitHub Actions workflow, which runs the scrapers
on a GitHub runner and commits `data/sku_counts.csv` back to this repo.
An optional Cloud Run fallback exists for heavy debugging loops. See
`SETUP.md` to replicate this in your own account, and `CLAUDE.md` for
how each scraper works and the site-specific gotchas.

## Setup

```bash
pip install -r requirements.txt
playwright install chromium
```

## Run

```bash
python main.py              # default: 4 parallel browser workers
python main.py --workers 8  # more parallelism (watch RAM/CPU)
```

Prints a summary table and appends rows to `data/sku_counts.csv`
(columns: `timestamp_utc, site, category, sku_count, status, note`).
Each site gets one `category=all` row for its whole catalog; sites with
category support (currently Sport Outlet) also get one row per top-level
category. A failed read is recorded with an empty `sku_count` and
`status=error` rather than a guessed number. Existing CSVs from before
the `category` column are migrated in place on the next run.

Sport Outlet's counts come from the site's own categories API
(`/api/v1/categories`, field `articlesCount`) rather than scraping
rendered tiles — the rendered catalog caps out around ~1700 tiles no
matter how far you scroll, so DOM counts undercut large categories. Its
`all` row is the sum over the API's main groups; a product listed under
two main groups is counted twice there.

## Adding / fixing a site scraper

Each site has its own module in `scrapers/` exporting a single function:

```python
def get_sku_count(page: playwright.sync_api.Page) -> int: ...
```

The shared helper `count_products_by_pagination` in `scrapers/_common.py`
handles the common case: it navigates a catalog page, counts unique
product-detail links matching a regex, and pages forward until no new
products appear. It supports two pagination styles:

- URL-param pagination (`?page=N`) via `next_page_url=lambda base, n: ...`
- A "Load more" / infinite-scroll button via `load_more_selector="..."`

New site scrapers currently raise `NotImplementedError` until their
catalog URL, product-link pattern, and pagination style have been
verified against the live site — see the TODO comment at the top of each
file in `scrapers/`.
