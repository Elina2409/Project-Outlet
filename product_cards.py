"""Crawl every canonical product page of a site and extract its card.

Usage: python product_cards.py {fjellsport|loplabbet} [--workers N] [--limit N]

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

loplabbet (verified via probe_source.py, 2026-07-07): one flat
sitemap.xml; product pages are root-level slugs carrying a
-dame-/-herre-/-unisex- token (content pages and model landing pages
like /adidas-boston-13 lack it). Each product page embeds
`"parentId":"<brand>-<articlecode>"` (e.g. dynafit-08-0000064118) in
its RSC JSON - that is the stable article id and goes in the
image_article column; brand is the parentId minus the trailing code;
name is og:title minus the " | Løplabbet.no" suffix.

Writes one row per product page to data/product_cards_<site>.csv
(committed by the scrape workflow when dispatched with the
product_cards input) and prints a dedupe summary: unique pages vs
unique article/image ids vs unique brand+name, with example duplicate
groups.
"""
from __future__ import annotations

import argparse
import csv
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests

from scrapers._common import USER_AGENT
from scrapers.fjellsport import SITEMAP_INDEX_URL, _SITEMAP_LOC_RE

DATA_DIR = Path(__file__).resolve().parent / "data"
FIELDS = ["url", "brand", "name", "sizes", "image_article", "status"]

LOPLABBET_SITEMAP_URL = "https://loplabbet.no/sitemap.xml"
# site -> (default workers, default per-request delay). fjellsport
# throttles hard (see module docstring); loplabbet's robots.txt allows
# fast crawling, so start quicker - the 429 backoff still protects it.
SITE_TUNING = {"fjellsport": (2, 0.4), "loplabbet": (4, 0.2)}

_OG_TITLE_RE = re.compile(r'property="og:title" content="([^"]*)"')
_TITLE_RE = re.compile(r"<title>([^<]*)</title>")
_OG_IMAGE_RE = re.compile(r'property="og:image" content="([^"]*)"')
_SELECTOR_LABEL_RE = re.compile(r'"selectorLabel":"([^"]*)"')
# Blob file name up to the trailing content hash:
# .../sw002479k18-hero-2e24cc4911.png -> sw002479k18
_IMAGE_ARTICLE_RE = re.compile(r"/([^/]+?)(?:-hero)?-[0-9a-f]{8,}\.\w+(?:\?|$)")

# loplabbet: gender token that marks a product slug, and the embedded
# parent product id (appears backslash-escaped inside the RSC JSON).
_GENDER_TOKEN_RE = re.compile(r"(?:^|-)(dame|herre|unisex)(?:-|$)")
_PARENT_ID_RE = re.compile(r'\\?"parentId\\?":\\?"([^"\\]+)')
_PARENT_CODE_RE = re.compile(r"^(?P<brand>.+?)-(?P<code>\d{2}-\d{6,}|\d{4,})$")
# Style/article code = slug tail after the LAST gender token
# (e.g. ...-dame-1204311b -> 1204311b, ...-unisex-08-0000064118 ->
# 08-0000064118). Matches the code half of parentId where both exist,
# and is present even on older products whose page omits parentId
# (~54% of the catalogue, verified 2026-07-07).
_SLUG_CODE_RE = re.compile(r"-(?:dame|herre|unisex)-([a-z0-9-]+)$")

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


def product_urls_loplabbet(session: requests.Session) -> list[str]:
    """Product URLs from the flat sitemap: root-level slugs carrying a
    gender token. Content prefixes (/artikler, /kampanjer, /dame, ...)
    and model landing pages (/adidas-boston-13) lack the token; the
    bare /dame and /herre listing pages are excluded by the exact
    match. The sitemap lists some products twice - the seen-set
    dedupes."""
    xml = session.get(LOPLABBET_SITEMAP_URL, timeout=60).text
    urls: list[str] = []
    seen: set[str] = set()
    for loc in _SITEMAP_LOC_RE.findall(xml):
        clean = loc.split("?")[0]
        parts = [p for p in clean.split("://", 1)[-1].split("/") if p][1:]
        if (len(parts) == 1 and parts[0] not in ("dame", "herre", "unisex")
                and _GENDER_TOKEN_RE.search(parts[0]) and clean not in seen):
            seen.add(clean)
            urls.append(clean)
    return urls


PRODUCT_URLS = {
    "fjellsport": product_urls_fjellsport,
    "loplabbet": product_urls_loplabbet,
}


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
            row["name"] = title.group(1).strip() if title else ""
            if site == "fjellsport":
                row["sizes"] = ";".join(
                    dict.fromkeys(_SELECTOR_LABEL_RE.findall(html))
                )
                image = _OG_IMAGE_RE.search(html)
                if image:
                    match = _IMAGE_ARTICLE_RE.search(image.group(1))
                    if match:
                        row["image_article"] = match.group(1)
            else:  # loplabbet
                row["name"] = row["name"].removesuffix(" | Løplabbet.no").strip()
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
                    # slug up to the first gender token.
                    code = _SLUG_CODE_RE.search(slug)
                    brand = slug.split("-dame-")[0].split("-herre-")[0] \
                        .split("-unisex-")[0].split("-")[0]
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
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("site", choices=sorted(PRODUCT_URLS))
    parser.add_argument("--workers", type=int, default=0,
                        help="parallel workers (default: per-site tuning)")
    parser.add_argument("--delay", type=float, default=-1.0,
                        help="seconds to sleep before each request, per worker")
    parser.add_argument("--limit", type=int, default=0,
                        help="crawl only the first N product pages (smoke test)")
    args = parser.parse_args()
    workers = args.workers or SITE_TUNING[args.site][0]
    delay = args.delay if args.delay >= 0 else SITE_TUNING[args.site][1]

    session = requests.Session()
    session.headers["User-Agent"] = USER_AGENT

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
