# CLAUDE.md — Nordic Sport Retail SKU Tracker

Counts the number of SKUs (products) listed on Norwegian sport retail
websites, whole-catalog and per category. Runs on demand — locally, via a
GitHub Actions workflow that commits the CSV back to this repo, or (as a
debugging fallback) as a GCP Cloud Run Job. No scheduler by default; a
cron line in the scrape workflow can be uncommented for scheduled runs.

## Key files

| File | Purpose |
|---|---|
| `main.py` | Entry point: runs every site scraper in a thread pool (`--workers`, default 4), appends rows to `data/sku_counts.csv`, prints a summary table. Exits 1 only if *every* row errored. |
| `scrapers/_common.py` | Shared helpers: headless browser launch, consent-banner dismissal, scroll/pagination tile counters, CSV writer (with in-place schema migration). |
| `scrapers/<site>.py` | One module per site key. Exports `get_sku_count(page)`; optionally `get_category_counts(page)` (direct counts, e.g. API) or `get_categories(page)` + `get_sku_count(page, url)` (per-URL counting). |
| `debug_page.py` | Diagnostics: loads a URL in the same headless browser and dumps title, buttons, imgs/links, category-name DOM shapes, intercepted apptus API label/count pairs, and the Sport Outlet categories API. Saves screenshot + HTML. |
| `probe_source.py` | Diagnostics for unreachable sites: dumps where a page's category/count data lives (script JSON blobs, count-keyed fields, nav links, term contexts). Run it in Cloud Run via `deploy.yml`'s `probe_url` input and read the run log. |
| `.github/workflows/scrape-sku.yml` | Manual-dispatch scrape on a GitHub runner; commits `data/`. Cron line included but commented out. |
| `.github/workflows/deploy.yml` | Dispatch-only GCP fallback: build image → deploy Cloud Run Job → optionally execute and print logs. Placeholders until `setup-gcp-wif.sh` output is pasted in. |
| `setup-gcp-wif.sh` | One-time, idempotent GCP setup (WIF keyless auth, service accounts, roles). |
| `SETUP.md` | Replication guide for a new GitHub account (+ optional GCP project). |
| `data/sku_counts.csv` | Output: one timestamped row per site/category per run. Committed by CI bots — never hand-edit. |

## Running

```bash
pip install -r requirements.txt && playwright install chromium
python main.py              # full snapshot, appends to data/sku_counts.csv
python debug_page.py <url>  # diagnose a page a scraper mis-reads
```

CSV columns: `timestamp_utc, site, category, sku_count, status, note`.
`category=all` is the whole catalog. A failed read is an empty
`sku_count` with `status=error` — never a guessed or zero value.
Per-category counts can sum to more than `all` (products live in
several categories).

## How each site is counted (and why)

- **Prefer a site's own catalog API over counting rendered tiles.**
  Both implemented sites turned out to expose one, and in both cases the
  rendered page lies (see gotchas).
- `sportoutlet` — GET https://sportoutlet.no/api/v1/categories. Main
  groups are dicts with `Name` + `articlesCount` and **no**
  `ArticleGroup2ID` (that key marks subgroups). `all` = sum over main
  groups. No browser navigation needed at all.
- `xxl` — data comes from the Apptus eSales storefront API
  (`*.api.esales.apptus.cloud`, landing-page/PLP query). Its URL embeds
  per-visitor `customerKey`/`sessionKey`, so never call it directly:
  load a category page and intercept the site's own responses
  (`page.on("response", ...)`). The navigation tree node
  `{path: "/", label: "All", count: N}` is the site-wide total; the node
  whose `label` matches the category name carries that category's count
  (`selected: true` nodes win; count-less breadcrumb entries are
  ignored). Category URLs come from the homepage top nav by visible link
  text. Kampanjer/Varemerker/Outlet are deliberately excluded.
- `sport1` — each category page (e.g. /klaer) renders the site's own
  total above the grid: `<span class="text-secondary">N</span>
  produkter`. That label is read directly (poll up to 30s; body-text
  fallback on the same pattern). Category URLs come from the homepage
  top nav by visible link text; Nyheter/Merker/Kampanjer/Outlet are
  deliberately excluded. No known whole-catalog page → the `all` row is
  an error row by design (summing overlapping categories would
  overcount).
- `fjellsport` — the homepage source embeds the full category tree with
  `"url":"/X","name":"N","productCount":C` per node (subcategories
  included). One plain HTTP fetch, no rendering. Five assortment
  categories pinned by URL path (`/herreklaer`, `/dameklaer`,
  `/turutstyr`, `/fottoy`, `/barn`). SALG/Nyheter/Fjellsportpris/
  Outlet/Varemerker/Aktiviteter excluded — Aktiviteter regroups ~88% of
  the catalog by activity (15727 vs ~17940 summed, 2026-07-03). No
  known site total → `all` is an error row by design.
- `antonsport`, `intersport` — `NotImplementedError` stubs.
  Check for a catalog API in the browser Network tab first (filter
  "api"); only fall back to DOM counting via the helpers in
  `_common.py`.

## Hard-won gotchas — read before debugging

- **Rendered catalogs cap out.** Sport Outlet stops loading tiles around
  ~1700 no matter how far you scroll; scroll-based counts of anything
  larger are silently wrong (API said Klær=4275, DOM gave 1732). Always
  validate a big category against the site's own numbers before trusting
  DOM counts.
- **Sport Outlet slugs are scrambled.** `/kj%C3%A6ledyr` serves the Klær
  category; the page `<h1>` echoes the slug even when wrong. Only the
  `<title>` ("Alle produkter i <name> - …") / breadcrumb self-link tell
  the truth. Never derive category identity from a slug or `<h1>`.
- **XXL's "Artikler: N" label never renders in headless runs** even
  though the API calls flow — waiting for it times out on every
  category. Counts must come from the intercepted API (the label is only
  a fallback).
- **No-image tiles vanish from counts.** A Sport Outlet tile whose CDN
  image fails shows `/storage/no-image.png` (no article-id) — dedupe
  keys need a fallback attribute (alt text) or counts jitter run-to-run.
- **Consent banners**: Norwegian sites run Cookiebot/similar;
  `dismiss_cookie_banner()` clicks "Tillat alle"/"Godta alle" variants.
  An undismissed banner can block rendering and scrolling.
- **The Claude Code cloud sandbox cannot reach these retail sites**
  (network policy 403s the CONNECT). Live verification needs Cloud
  Shell, the GitHub workflow, or user-pasted page details / DevTools
  captures — Network-tab screenshots from the user have been the
  highest-value input by far.
- **Playwright sync API is not thread-safe**: one Playwright instance +
  browser per worker thread, never shared.
- **Scheduled workflows only fire from the repo's default branch**;
  `workflow_dispatch` works from any branch (that's the debug loop).
- **Cloud Run containers are ephemeral** — the CSV written there is
  discarded. Persistence lives on the GitHub-runner path (bot commits);
  the GCP path is for log-based trial and error only.

## The debug loop (reuse it)

When the site can't be reached from where Claude runs: push the branch →
dispatch `scrape-sku.yml` on that branch from the Actions tab → read the
run log (`main.py` prints one status line per site/category; scraper
errors carry the reason) → adjust → repeat. `debug_page.py` output
pasted by a human closes the gap when DOM/API shapes are the question.
For a different egress network or heavier iteration, dispatch
`deploy.yml` with execute=true and read the Cloud Run logs it prints.

## Rules for Claude

- One site failing must never stop the others; every scraper call is
  wrapped and continues.
- A failed read is stored as NULL (`status=error`), never guessed/zero.
  A category that can't be resolved gets its own error row — it must not
  vanish silently.
- Timestamps are UTC, ISO-8601.
- Never guess pagination, selectors, slugs, or API JSON shapes. Verify
  against the live DOM/API (or user-provided captures) first. A
  `NotImplementedError` stub beats a plausible-looking wrong count.
- Don't commit secrets; GCP auth is keyless WIF (no stored keys).
- `data/` is written by CI bots; never hand-edit it.

## Sites (site keys, immutable)

- `sportoutlet` — sportoutlet.no (API-based, categories + all)
- `xxl` — xxl.no (intercepted eSales API, categories + all)
- `antonsport` — antonsport.no (stub)
- `intersport` — intersport.no (stub)
- `sport1` — sport1.no (rendered "N produkter" label, per-category only; no site-wide total)
- `fjellsport` — fjellsport.no (embedded productCount tree in homepage source, per-category only; no site-wide total)
