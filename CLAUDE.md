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
| `probe_source.py` | Diagnostics for unreachable sites: dumps where a page's category/count data lives (script JSON blobs, count-keyed fields, nav links, term contexts; sitemap indexes get a per-path-prefix URL census). Run it in Cloud Run via `deploy.yml`'s `probe_url` input and read the run log. |
| `product_cards.py` | Product-card crawler: fetches every canonical product URL from a site's sitemap via plain HTTP, extracts the card (brand, name, sizes, og:image article id) and writes `data/product_cards_<site>.csv` plus a duplicate summary. Run via `scrape-sku.yml`'s `product_cards` input (site key) so the CSV is committed. |
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
  Product-card duplicate detection uses a separate, unrelated platform —
  a dedicated product-only sitemap plus schema.org JSON-LD
  (`product_cards.py xxl`) — see gotchas below.
- `sport1` — each category page (e.g. /klaer) renders the site's own
  total above the grid: `<span class="text-secondary">N</span>
  produkter`. That label is read directly (poll up to 30s; body-text
  fallback on the same pattern). Category URLs come from the homepage
  top nav by visible link text; Nyheter/Merker/Kampanjer/Outlet are
  deliberately excluded. No known whole-catalog page → the `all` row is
  an error row by design (summing overlapping categories would
  overcount). Same parentId-based commerce platform as loplabbet/
  intersport (`product_cards.py sport1`) — see gotchas below; sport1 and
  intersport evidently share the same underlying product catalog.
- `fjellsport` — the homepage source embeds the full category tree with
  `"url":"/X","name":"N","productCount":C` per node (subcategories
  included). One plain HTTP fetch, no rendering. Five assortment
  categories pinned by URL path (`/herreklaer`, `/dameklaer`,
  `/turutstyr`, `/fottoy`, `/barn`). SALG/Nyheter/Fjellsportpris/
  Outlet/Varemerker/Aktiviteter excluded — Aktiviteter regroups ~88% of
  the catalog by activity (15727 vs ~17940 summed, 2026-07-03). `all`
  is the count of unique canonical product pages
  (`/merker/<brand>/<slug>`, depth ≥ 3 under `/merker/`) across the
  sitemap files (index at `/api/sitemap/nb-no/sitemapindex.xml`, named
  by robots.txt) — the only genuinely deduplicated site total in the
  tracker. Product slugs can encode color variants, so it may exceed
  the category tree's counts; robots.txt's `Disallow: /produkter/` does
  NOT match the canonical product URLs.
- `intersport` — same platform as loplabbet (Excite-like, embeds
  `"parentId":"<brand>-<code>"` per product). No embedded category-tree
  count and no verified CSS selector for the count element, so counts
  come from the rendered body text: `<N> PRODUKTER` next to the filter
  bar, also appears as `VISER 15 AV <N> PRODUKTER` in pagination
  (`probe_source.py`, 2026-07-08). Category URLs from the top nav:
  Klær, Sko, Sykkel, Sport og ballspill, Friluft, Trening og helse,
  Vintersport; Merker/Nyheter(kolleksjon)/Kampanjer/Outlet excluded as
  cross-cutting. No known whole-catalog page → `all` is an error row by
  design.
- `antonsport` — `NotImplementedError` stub.
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
- **Fjellsport rate-limits crawls.** 8 workers with no delay got HTTP
  429 on 94% of 21550 product-page fetches; 2 workers + 0.4s delay with
  Retry-After backoff completed with 1 failure (~100 min). Any
  full-catalog crawl must throttle and must fail the run when >10% of
  fetches fail — a partial crawl must never pass as the answer.
- **Fjellsport's `selectorLabel` is a size label, not an article code**
  ("S"/"M"/"XL" — thousands of unrelated products share "S;M;L;XL").
  Product-card dedupe keys that work: og:image blob id (e.g.
  `sw002479k18`) and brand+og:title. Beware generic blob names ("1",
  "unnamed", "png-2000px-max-72dpi") grouping unrelated products —
  require image AND name to agree for high-confidence duplicates.
  Verified 2026-07-06: 21549 pages → ~40 duplicate groups (56 extra
  pages, mostly size variants with their own pages); ≈21493 unique.
- **Loplabbet is crawl-friendly and mostly variant-per-page.** One flat
  `sitemap.xml` (~4450 URLs); product pages are root-level slugs with a
  `-dame-/-herre-/-unisex-` token (content prefixes and model landing
  pages like `/adidas-boston-13` lack it). No 429s at 4 workers/0.2s.
  Article/style code = slug tail after the last gender token
  (`...-dame-1204311b` -> `1204311b`), equal to the code half of the
  embedded RSC `parentId` (`brooks-sports-1204311b`) where that exists
  — but ~54% of pages (older products) omit `parentId`, so derive the
  code from the URL, not just the page JSON. Verified 2026-07-07: 3961
  product pages, 0 failures; 3937 unique by brand+name (24 exact
  duplicates); ~2771 unique style codes (592 models carry ≥2 colour
  variants as their own pages — e.g. Nike Zoom Fly 6 in 14 colours).
- **Intersport's product slugs use more audience tokens than loplabbet's.**
  Same platform and sitemap shape, but besides `-dame-/-herre-/-unisex-`
  it also uses `-barn-` (kids) and `-alle-` (universal audience — pet
  gear, accessories). The first crawl matched only the loplabbet token
  set and silently dropped ~40% of the catalog (20507 of an expected
  ~35k pages) — caught by the gap between the crawl total and the
  category-page totals (Klær alone shows 8707). `product_cards.py`
  keeps a per-site `GENDER_TOKENS` set for exactly this reason; when
  adding a new site on this platform, verify its full token set against
  the sitemap census before trusting a crawl total.
- **Sport1 and Intersport appear to be the same underlying catalog.**
  A product-page probe on sport1.no (2026-07-09) confirmed the identical
  `"parentId":"<brand>-<code>"` JSON shape used by loplabbet/intersport,
  so `product_cards.py` reused that platform's extraction logic wholesale
  (same flat-sitemap crawl, same 5-token `GENDER_TOKENS`, applied from
  the start this time — no repeat of the intersport undercount). Full
  crawl (2026-07-09): 39957 product pages, 0 failures; 25869 unique
  article/parentId codes (5228 duplicate groups, mostly colour variants
  sharing one code — e.g. `2xu-wr7369a` in black/white); 37485 unique by
  brand+name (2041 near-duplicate groups, e.g. the same lock listed under
  two different article codes). 591 distinct brands; top by SKU count:
  Bergans (3462), Jotunheim (2144), Adidas (2084), Rapala (1461),
  Sølvkroken (1189). Cross-checked against intersport's crawl: 15825 of
  sport1's 25869 article codes (61%) are byte-identical to codes on
  intersport.no — strong evidence the two sites resell the same
  wholesaler catalog under separate storefronts, not just the same
  platform vendor.
- **xxl uses a completely different product-card platform** (schema.org
  `ProductGroup`/`hasVariant` JSON-LD, not the parentId RSC pattern) and
  is the only site of the five where `product_cards.py`'s own crawl
  needs to walk a dedicated *product-only* sitemap index
  (`sitemaps/auto/live-product/sitemapindex.xml`, from robots.txt) with
  no audience-token filtering — every child sitemap is product pages
  already. Verified via probe_source.py against a live product page
  (2026-07-13): `productGroupId` matches the URL's numeric id and is the
  dedupe key; `variesBy: ["schema.org/size"]` confirms size stays on one
  page here too. Full crawl: 23870 pages, 4 failures; 22898 unique
  productGroupIds (509 duplicate groups — occasionally two colours share
  one id, e.g. `1163833` in yellow/black); 16285 unique by brand+name —
  much lower than the other four sites' brand+name counts relative to
  page count, because xxl's JSON `name` field never includes colour
  (e.g. "Court Vision Low Next Nature, sneaker, dame" has no colour
  word) — so brand+name collapses every colour of a model into one
  group here, the opposite of what article-code dedupe does on the
  parentId platform. 580 distinct brands; top by SKU count: Nike (2981),
  Adidas (1281), Puma (1103), Timberland (993), Stormberg (878).
- **Page count is the only truly apples-to-apples "SKU" number across
  all five sites** — every one of them keeps size as an in-page
  attribute (never its own URL), and every one gives each colour its own
  URL, so page count = colour-variant count everywhere. The "unique
  article/style code" column is NOT comparable across sites: it's
  near-colour-level for fjellsport and xxl (few duplicate groups), but
  frequently colour-blind (one code covers several colours) for the
  parentId platform (loplabbet/intersport/sport1), so its number sits
  much closer to "unique models" there. "Unique brand+name" also isn't
  comparable as-is: it's colour-blind by construction on xxl (colour
  isn't in the name field at all) but roughly colour-level on the other
  four (colour is usually part of the title/og:title text). Verified
  2026-07-13 by loading all five `product_cards_<site>.csv` files
  side by side:

  | site | pages (=colours) | unique brand+name | unique article/style code |
  |---|---|---|---|
  | fjellsport | 21549 | 21205 | 20397 |
  | loplabbet | 3961 | 3916 | 2784 |
  | intersport | 32865 | 31084 | 22613 |
  | sport1 | 39957 | 37485 | 25869 |
  | xxl | 23866 | 16285 | 22898 |
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
- `intersport` — intersport.no (rendered "N PRODUKTER" body text, per-category only; no site-wide total)
- `sport1` — sport1.no (rendered "N produkter" label, per-category only; no site-wide total)
- `fjellsport` — fjellsport.no (embedded productCount tree in homepage source; deduplicated site total from the product sitemap)
