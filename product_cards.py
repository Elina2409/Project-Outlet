"""Crawl every canonical product page of a site and extract its card.

Usage: python product_cards.py {fjellsport|loplabbet|intersport|sport1|xxl} [--workers N] [--limit N]

Answers "are there duplicate products behind distinct URLs?" - the
sitemap total (scrapers/fjellsport.py) is deduplicated by URL only, so
the same physical product could still hide behind two slugs, and color
variants may have pages of their own. Each product page embeds a
product JSON in its source (verified via probe_source.py, 2026-07-06)
carrying the fields extracted here:

- sizes: `"selectorLabel":"..."` entries (size variants of the shown
  color - NOT article codes, as the first live run proved: thousands of
  unrelated products share "S;M;L;XL")
- image article id: the og:image blob name, e.g.
  sw002479k18-hero-2e24cc4911.png -> sw002479k18 (style+color code) -
  the primary dedupe key
- name: og:title; brand: the /merker/<brand>/ URL segment

Plain HTTP fetches, no browser - but POLITELY: the first run at 8
workers with no delay got HTTP 429 on 94% of requests. Defaults are now
2 workers + 0.4s delay per request (~70-90 min for the full crawl), and
429s are retried with backoff honoring Retry-After.

loplabbet, intersport and sport1 (verified via probe_source.py,
2026-07-07 and 2026-07-08, and a product-page probe on
atomic-backland-expert-blackdark-blue-herre-ae5027400 on 2026-07-09):
same commerce platform - sport1 and intersport's sitemap censuses even
share byte-identical product slugs, so they evidently sell out of the
same underlying catalog. One flat sitemap.xml per site; product pages
are root-level slugs carrying an audience token (see GENDER_TOKENS -
loplabbet: dame/herre/unisex; intersport and sport1 additionally carry
barn/alle for kids' and universal-audience products, e.g. pet gear -
the first intersport crawl used only dame/herre/unisex and silently
missed ~40% of the catalog, caught 2026-07-08 by cross-checking the
crawled count against the category-page totals - sport1's census was
checked against the full 5-token set from the start). Content/category
pages like /klaer, /kampanjer and model landing pages like
/adidas-boston-13 lack any audience token. Each product page embeds
`"parentId":"<brand>-<articlecode>"` (e.g. dynafit-08-0000064118,
atomic-ae5027400) in its RSC JSON - the stable article id, goes in the
image_article column; brand is the parentId minus the trailing code;
name is og:title with the trailing " | <Site name>" stripped.

xxl (verified via probe_source.py, 2026-07-13): a different commerce
platform entirely (Apptus eSales storefront, same one scrapers/xxl.py
intercepts for category counts - unrelated to this file's crawl, which
is a separate plain-HTTP sitemap walk). Its sitemap index
(sitemaps/auto/live-product/sitemapindex.xml, from robots.txt) points
at dozens of child sitemaps that are product pages ONLY - no audience-
token filtering needed, unlike the parentId platform. Each product page
embeds a proper schema.org `<script type="application/ld+json">` block
(a JSON array containing a ProductGroup object, not escaped RSC JSON):
`productGroupId` (matches the URL's numeric id, e.g. .../p/1250508_1_Style)
is the dedupe key; `brand.name` and `name` are read straight from the
JSON; sizes come from `hasVariant[].size` - confirmed size stays on one
page (`variesBy: ["https://schema.org/size"]`), same as every other site
here, so page count is directly SKU-comparable across all five sites.

sportoutlet (verified via probe_source.py, 2026-07-30): no per-product
crawl at all - there's no sitemap of product pages, and no product
detail page seems to exist (no url/slug field on any article record,
and no product-tile links anywhere in the rendered DOM). Instead reads
the WHOLE catalog directly from the site's own Elasticsearch-backed
search API (POST /api/v1/articles/search, paginated via take/page,
empty "filters" returns everything): hits.total is the site's own
exact, deduplicated count of ARTICLES - 5830, notably below the 8050
you get by summing scrapers/sportoutlet.py's per-category
articlesCount (which double-counts articles cross-listed in more than
one category) - but that 5830 is article-level, not colour-level: one
article's own `colors` array routinely bundles several colours (one
record listed 19), unlike every other site here where each colour gets
its own page. So this crawl unrolls `colors` into one row per colour -
image_article is `ArticleUUID:ColorID`, and the colour name is folded
into `name` - to make the row count comparable to the other five sites'
page-count-is-colour-count metric; don't use the raw article total for
that comparison. The API needs a Laravel CSRF double-submit: a plain
GET first to receive the XSRF-TOKEN cookie, then echo its (URL-decoded)
value back as the X-XSRF-TOKEN header on the POST, or every call 419s.
The `url` column is left blank for this site - there is nothing real
to put there, and guessing one would violate this project's "never
guess a URL" rule.

Writes one row per product page to data/product_cards_<site>.csv
(committed by the scrape workflow when dispatched with the
product_cards input) and prints a dedupe summary: unique pages vs
unique article/image ids vs unique brand+name, with example duplicate
groups.
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import unquote

import requests

from scrapers._common import USER_AGENT, browser_page
from scrapers.fjellsport import SITEMAP_INDEX_URL, _SITEMAP_LOC_RE

DATA_DIR = Path(__file__).resolve().parent / "data"
FIELDS = ["url", "brand", "name", "sizes", "image_article", "status"]

# Flat-sitemap sites sharing one commerce platform (parentId-based).
FLAT_SITEMAP_URLS = {
    "loplabbet": "https://loplabbet.no/sitemap.xml",
    "intersport": "https://www.intersport.no/sitemap.xml",
    "sport1": "https://www.sport1.no/sitemap.xml",
}
# site -> (default workers, default per-request delay). fjellsport
# throttles hard (see module docstring); loplabbet/intersport/sport1's
# robots.txt allows fast crawling, so start quicker - the 429 backoff
# still protects all three. Intersport's and sport1's sitemaps are
# ~9-11x loplabbet's size (~35-45k vs ~4k pages), so budget roughly
# 1.5-2.5h even at 4 workers.
SITE_TUNING = {"fjellsport": (2, 0.4), "loplabbet": (4, 0.2),
               "intersport": (4, 0.2), "sport1": (4, 0.2), "xxl": (4, 0.2)}

# xxl's dedicated product-only sitemap index (from robots.txt, 2026-07-13).
XXL_SITEMAP_INDEX_URL = "https://www.xxl.no/sitemaps/auto/live-product/sitemapindex.xml"
_XXL_LDJSON_RE = re.compile(
    r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>', re.DOTALL
)

_OG_TITLE_RE = re.compile(r'property="og:title" content="([^"]*)"')
_TITLE_RE = re.compile(r"<title>([^<]*)</title>")
_OG_IMAGE_RE = re.compile(r'property="og:image" content="([^"]*)"')
_SELECTOR_LABEL_RE = re.compile(r'"selectorLabel":"([^"]*)"')
# Blob file name up to the trailing content hash:
# .../sw002479k18-hero-2e24cc4911.png -> sw002479k18
_IMAGE_ARTICLE_RE = re.compile(r"/([^/]+?)(?:-hero)?-[0-9a-f]{8,}\.\w+(?:\?|$)")

# Audience token that marks a product slug on the parentId-based
# platform. loplabbet only ever showed dame/herre/unisex; intersport's
# and sport1's sitemaps also carry -barn- (kids) and -alle- (all-audience:
# pet gear, universal accessories) - missing these on the first
# intersport crawl silently dropped ~40% of its catalog (verified
# 2026-07-08). sport1's census (44,942 URLs) was checked for the full
# 5-token set up front to avoid repeating that mistake.
GENDER_TOKENS = {
    "loplabbet": ("dame", "herre", "unisex"),
    "intersport": ("dame", "herre", "unisex", "barn", "alle"),
    "sport1": ("dame", "herre", "unisex", "barn", "alle"),
}
_PARENT_ID_RE = re.compile(r'\\?"parentId\\?":\\?"([^"\\]+)')
_PARENT_CODE_RE = re.compile(r"^(?P<brand>.+?)-(?P<code>\d{2}-\d{6,}|\d{4,})$")


def _gender_token_re(site: str) -> re.Pattern:
    tokens = "|".join(GENDER_TOKENS[site])
    return re.compile(rf"(?:^|-)({tokens})(?:-|$)")


def _slug_code_re(site: str) -> re.Pattern:
    """Style/article code = slug tail after the LAST audience token
    (e.g. ...-dame-1204311b -> 1204311b, ...-unisex-08-0000064118 ->
    08-0000064118). Matches the code half of parentId where both
    exist, and is present even on older products whose page omits
    parentId (~54% of loplabbet's catalogue, verified 2026-07-07)."""
    tokens = "|".join(GENDER_TOKENS[site])
    return re.compile(rf"-(?:{tokens})-([a-z0-9-]+)$")

_progress_lock = threading.Lock()
_done = 0


def product_urls_fjellsport(session: requests.Session) -> list[str]:
    """All canonical product URLs (/merker/<brand>/<slug>) from the
    sitemap files - same selection as scrapers/fjellsport.py."""
    index = session.get(SITEMAP_INDEX_URL, timeout=30).text
    urls: list[str] = []
    seen: set[str] = set()
    for sitemap_url in _SITEMAP_LOC_RE.findall(index):
        xml = session.get(sitemap_url, timeout=60).text
        for loc in _SITEMAP_LOC_RE.findall(xml):
            clean = loc.split("?")[0]
            parts = [p for p in clean.split("://", 1)[-1].split("/") if p][1:]
            if len(parts) >= 3 and parts[0] == "merker" and clean not in seen:
                seen.add(clean)
                urls.append(clean)
    return urls


def product_urls_flat_sitemap(session: requests.Session, sitemap_url: str, site: str) -> list[str]:
    """Product URLs from a flat sitemap: root-level slugs carrying an
    audience token (see GENDER_TOKENS). Content/category prefixes
    (/artikler, /kampanjer, /klaer, /dame, ...) and model landing pages
    (/adidas-boston-13) lack the token; the bare listing pages (/dame,
    /barn, ...) are excluded by the exact match. The sitemap lists some
    products twice - the seen-set dedupes."""
    tokens = GENDER_TOKENS[site]
    gender_re = _gender_token_re(site)
    xml = session.get(sitemap_url, timeout=60).text
    urls: list[str] = []
    seen: set[str] = set()
    for loc in _SITEMAP_LOC_RE.findall(xml):
        clean = loc.split("?")[0]
        parts = [p for p in clean.split("://", 1)[-1].split("/") if p][1:]
        if (len(parts) == 1 and parts[0] not in tokens
                and gender_re.search(parts[0]) and clean not in seen):
            seen.add(clean)
            urls.append(clean)
    return urls


def product_urls_xxl(session: requests.Session) -> list[str]:
    """All product URLs from xxl's live-product sitemap index. Unlike the
    other sites' general sitemaps this one is product pages only (verified
    2026-07-13), so no audience-token filtering is needed - every child
    sitemap is fetched in full (the 20-file cap in probe_source.py is a
    diagnostic-only safeguard, not appropriate for an actual crawl)."""
    index = session.get(XXL_SITEMAP_INDEX_URL, timeout=30).text
    urls: list[str] = []
    seen: set[str] = set()
    for child_url in _SITEMAP_LOC_RE.findall(index):
        xml = session.get(child_url, timeout=60).text
        for loc in _SITEMAP_LOC_RE.findall(xml):
            clean = loc.split("?")[0]
            if "/p/" in clean and clean not in seen:
                seen.add(clean)
                urls.append(clean)
    return urls


def _xxl_product_group(html: str) -> dict | None:
    """The schema.org ProductGroup object from the page's ld+json block."""
    for match in _XXL_LDJSON_RE.finditer(html):
        try:
            data = json.loads(match.group(1))
        except ValueError:
            continue
        for item in data if isinstance(data, list) else [data]:
            if isinstance(item, dict) and item.get("@type") == "ProductGroup":
                return item
    return None


PRODUCT_URLS = {
    "fjellsport": product_urls_fjellsport,
    "xxl": product_urls_xxl,
    **{
        site: (lambda session, url=url, site=site: product_urls_flat_sitemap(session, url, site))
        for site, url in FLAT_SITEMAP_URLS.items()
    },
}

SPORTOUTLET_BASE = "https://sportoutlet.no"
SPORTOUTLET_SEARCH_URL = f"{SPORTOUTLET_BASE}/api/v1/articles/search"


def _sportoutlet_csrf_token(page) -> str:
    """Laravel's CSRF double-submit: any page load sets an XSRF-TOKEN
    cookie that must be echoed back as the X-XSRF-TOKEN header on POSTs,
    or /api/v1/articles/search returns 419 (verified via probe_source.py,
    2026-07-30). A plain `requests` GET here gets a hard 30s connect
    timeout from GitHub Actions runners (verified 2026-07-30) even though
    Cloud Run reaches it fine every time - reads as a TLS/client
    fingerprint block, so this goes through a real Playwright browser
    context instead, same as scrapers/sportoutlet.py's existing API call."""
    page.request.get(SPORTOUTLET_BASE, timeout=30000)
    for cookie in page.context.cookies():
        if cookie["name"] == "XSRF-TOKEN":
            return unquote(cookie["value"])
    raise RuntimeError("sportoutlet.no set no XSRF-TOKEN cookie - can't call articles/search")


def fetch_cards_sportoutlet(page) -> list[dict]:
    """Every article straight from the site's own search API - see the
    module docstring's sportoutlet section for why there's no per-page
    crawl here. Paginates with take/page until a page comes back short.

    One row per *colour*, not per article (verified via probe_source.py,
    2026-07-30: a single article's own `colors` array routinely lists
    several colours - one record had 19 - so the raw article count
    (hits.total, 5830) is coarser than "colour variant" and NOT
    comparable to the other five sites' page-count-is-colour-count
    metric). Unrolling `colors` here is what makes the row count
    apples-to-apples with them: image_article is ArticleUUID:ColorID
    (unique per colour, same idea as the og:image-hash keys used
    elsewhere) and the colour name is folded into `name`, matching how
    the other sites' titles already read (colour as part of the name)."""
    headers = {"content-type": "application/json",
               "X-XSRF-TOKEN": _sportoutlet_csrf_token(page)}
    rows: list[dict] = []
    article_count = 0
    page_num = 0
    take = 1000
    total = None
    while True:
        resp = page.request.post(
            SPORTOUTLET_SEARCH_URL,
            data=json.dumps({"query": "", "take": take, "page": page_num, "filters": ""}),
            headers=headers, timeout=60000,
        )
        if not resp.ok:
            raise RuntimeError(f"articles/search returned HTTP {resp.status}")
        hits = resp.json().get("hits", {})
        if total is None:
            total = hits.get("total", {}).get("value")
            print(f"articles.hits.total (site-reported, article-level, NOT colour-level): {total}")
        batch = hits.get("hits", [])
        if not batch:
            break
        for hit in batch:
            source = hit.get("_source", {})
            article_count += 1
            article_uuid = str(source.get("ArticleUUID") or source.get("ArticleID") or "")
            brand = source.get("ProductLine") or ""
            name = source.get("Name") or ""
            colors = source.get("colors") or []
            if not colors:
                rows.append({"url": "", "brand": brand, "name": name,
                             "sizes": "", "image_article": article_uuid, "status": "ok"})
                continue
            for color in colors:
                color_name = color.get("Name") or color.get("name") or ""
                color_id = color.get("ColorID") if color.get("ColorID") is not None else color.get("id")
                rows.append({
                    "url": "",
                    "brand": brand,
                    "name": f"{name}, {color_name}" if color_name else name,
                    "sizes": "",
                    "image_article": f"{article_uuid}:{color_id}" if color_id is not None else article_uuid,
                    "status": "ok",
                })
        print(f"  ... fetched page {page_num} ({article_count} articles, "
              f"{len(rows)} colour-rows so far)", flush=True)
        page_num += 1
    return rows


def fetch_card(session: requests.Session, site: str, url: str,
               total: int, delay: float) -> dict:
    global _done
    row = {"url": url, "brand": "", "name": "", "sizes": "",
           "image_article": "", "status": "ok"}
    parts = [p for p in url.split("://", 1)[-1].split("/") if p][1:]
    if site == "fjellsport":
        row["brand"] = parts[1] if len(parts) > 1 else ""
    try:
        time.sleep(delay)
        for attempt in range(6):
            resp = session.get(url, timeout=30)
            if resp.status_code != 429:
                break
            # Throttled: honor Retry-After, else exponential backoff.
            wait = float(resp.headers.get("Retry-After") or 0) or 2**attempt
            time.sleep(min(wait, 60))
        if resp.status_code != 200:
            row["status"] = f"http {resp.status_code}"
        else:
            html = resp.text
            title = _OG_TITLE_RE.search(html) or _TITLE_RE.search(html)
            name = title.group(1).strip() if title else ""
            # Every site's <title>/og:title ends " | <Site Name>" - the
            # dedupe keys only care about the product name itself.
            row["name"] = name.split(" | ")[0].strip()
            if site == "fjellsport":
                row["sizes"] = ";".join(
                    dict.fromkeys(_SELECTOR_LABEL_RE.findall(html))
                )
                image = _OG_IMAGE_RE.search(html)
                if image:
                    match = _IMAGE_ARTICLE_RE.search(image.group(1))
                    if match:
                        row["image_article"] = match.group(1)
            elif site == "xxl":
                group = _xxl_product_group(html)
                if group:
                    row["name"] = group.get("name") or row["name"]
                    row["brand"] = (group.get("brand") or {}).get("name", "")
                    row["image_article"] = str(
                        group.get("productGroupId") or group.get("sku") or ""
                    )
                    sizes = {
                        v.get("size") for v in group.get("hasVariant", [])
                        if isinstance(v, dict) and v.get("size")
                    }
                    row["sizes"] = ";".join(sorted(sizes))
            else:  # loplabbet, intersport, sport1: same parentId-based platform
                slug = url.rstrip("/").split("/")[-1]
                parent = _PARENT_ID_RE.search(html)
                if parent:
                    # Cleanest id: brand-code straight from the page JSON.
                    parent_id = parent.group(1)
                    row["image_article"] = parent_id
                    split = _PARENT_CODE_RE.match(parent_id)
                    row["brand"] = split["brand"] if split else parent_id.split("-")[0]
                else:
                    # Older products omit parentId - rebuild the style id
                    # from the URL: <brand>-<slug-tail code>. Brand is the
                    # slug up to the first audience token.
                    code = _slug_code_re(site).search(slug)
                    brand = slug
                    for token in GENDER_TOKENS[site]:
                        brand = brand.split(f"-{token}-")[0]
                    brand = brand.split("-")[0]
                    row["brand"] = brand
                    if code:
                        row["image_article"] = f"{brand}-{code.group(1)}"
    except Exception as exc:
        row["status"] = f"error: {exc}"[:150]
    with _progress_lock:
        _done += 1
        if _done % 1000 == 0:
            print(f"  ... {_done}/{total} pages", flush=True)
    return row


def summarize(rows: list[dict]) -> None:
    ok = [r for r in rows if r["status"] == "ok"]
    failed = len(rows) - len(ok)
    print(f"\npages fetched : {len(rows)} ({failed} failed)")

    def name_key(r: dict) -> str:
        return f"{r['brand']}|{r['name'].casefold()}" if r["name"] else ""

    for key_fn, label in [(lambda r: r["image_article"],
                           "image article id (style+color)"),
                          (name_key, "brand+name")]:
        groups: dict[str, list[str]] = {}
        missing = 0
        for r in ok:
            value = key_fn(r)
            if not value:
                missing += 1
                continue
            groups.setdefault(value, []).append(r["url"])
        dupes = {v: u for v, u in groups.items() if len(u) > 1}
        print(f"\nby {label}:")
        print(f"  unique values : {len(groups)}  (pages without value: {missing})")
        print(f"  duplicate groups (same value, several URLs): {len(dupes)}")
        for value, urls in list(dupes.items())[:10]:
            print(f"    {value}:")
            for u in urls:
                print(f"      {u}")


def main() -> None:
    all_sites = sorted(set(PRODUCT_URLS) | {"sportoutlet"})
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("site", choices=all_sites)
    parser.add_argument("--workers", type=int, default=0,
                        help="parallel workers (default: per-site tuning)")
    parser.add_argument("--delay", type=float, default=-1.0,
                        help="seconds to sleep before each request, per worker")
    parser.add_argument("--limit", type=int, default=0,
                        help="crawl only the first N product pages (smoke test)")
    args = parser.parse_args()

    if args.site == "sportoutlet":
        # No per-page crawl for this site - see the module docstring.
        # Goes through a real browser context (not plain `requests`) -
        # GitHub Actions runners hard-timeout connecting to sportoutlet.no
        # otherwise (verified 2026-07-30), while Cloud Run reaches it fine
        # every time, which reads as a TLS/client fingerprint block.
        with browser_page() as page:
            rows = fetch_cards_sportoutlet(page)
        if args.limit:
            rows = rows[: args.limit]
            print(f"limited to first {len(rows)}")
    else:
        session = requests.Session()
        session.headers["User-Agent"] = USER_AGENT
        workers = args.workers or SITE_TUNING[args.site][0]
        delay = args.delay if args.delay >= 0 else SITE_TUNING[args.site][1]

        urls = PRODUCT_URLS[args.site](session)
        print(f"product URLs from sitemap: {len(urls)}")
        if args.limit:
            urls = urls[: args.limit]
            print(f"limited to first {len(urls)}")

        with ThreadPoolExecutor(max_workers=workers) as pool:
            rows = list(pool.map(
                lambda u: fetch_card(session, args.site, u, len(urls), delay), urls
            ))

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    out = DATA_DIR / f"product_cards_{args.site}.csv"
    with out.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    print(f"wrote {out} ({len(rows)} rows)")

    summarize(rows)

    # More than 10% failures means the crawl (and its dedupe numbers)
    # can't be trusted - mark the run red so nobody reads a partial
    # crawl as the answer (the first run silently lost 94% to HTTP 429).
    failed = sum(1 for r in rows if r["status"] != "ok")
    if rows and failed > len(rows) * 0.10:
        print(f"\nFAILED: {failed}/{len(rows)} fetches failed - partial crawl")
        sys.exit(1)


if __name__ == "__main__":
    main()
