"""Crawl every canonical fjellsport product page and extract its card.

Usage: python product_cards.py fjellsport [--workers N] [--limit N]

Answers "are there duplicate products behind distinct URLs?" - the
sitemap total (scrapers/fjellsport.py) is deduplicated by URL only, so
the same physical product could still hide behind two slugs, and color
variants may have pages of their own. Each product page embeds a
product JSON in its source (verified via probe_source.py, 2026-07-06)
carrying the fields extracted here:

- article codes: `"selectorLabel":"GP50120-1"` entries (one per size
  variant of the shown color)
- image article id: the og:image blob name, e.g.
  sw002479k18-hero-2e24cc4911.png -> sw002479k18 (style+color code)
- name: og:title; brand: the /merker/<brand>/ URL segment

Plain HTTP fetches, no browser. Writes one row per product page to
data/product_cards_<site>.csv (committed by the scrape workflow when
dispatched with the product_cards input) and prints a dedupe summary:
unique pages vs unique article codes vs unique image ids, with example
duplicate groups.
"""
from __future__ import annotations

import argparse
import csv
import re
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests

from scrapers._common import USER_AGENT
from scrapers.fjellsport import SITEMAP_INDEX_URL, _SITEMAP_LOC_RE

DATA_DIR = Path(__file__).resolve().parent / "data"
FIELDS = ["url", "brand", "name", "article_codes", "image_article", "status"]

_OG_TITLE_RE = re.compile(r'property="og:title" content="([^"]*)"')
_TITLE_RE = re.compile(r"<title>([^<]*)</title>")
_OG_IMAGE_RE = re.compile(r'property="og:image" content="([^"]*)"')
_SELECTOR_LABEL_RE = re.compile(r'"selectorLabel":"([^"]*)"')
# Blob file name up to the trailing content hash:
# .../sw002479k18-hero-2e24cc4911.png -> sw002479k18
_IMAGE_ARTICLE_RE = re.compile(r"/([^/]+?)(?:-hero)?-[0-9a-f]{8,}\.\w+(?:\?|$)")

_progress_lock = threading.Lock()
_done = 0


def product_urls(session: requests.Session) -> list[str]:
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


def fetch_card(session: requests.Session, url: str, total: int) -> dict:
    global _done
    row = {"url": url, "brand": "", "name": "", "article_codes": "",
           "image_article": "", "status": "ok"}
    parts = [p for p in url.split("://", 1)[-1].split("/") if p][1:]
    row["brand"] = parts[1] if len(parts) > 1 else ""
    try:
        resp = session.get(url, timeout=30)
        if resp.status_code != 200:
            row["status"] = f"http {resp.status_code}"
        else:
            html = resp.text
            title = _OG_TITLE_RE.search(html) or _TITLE_RE.search(html)
            row["name"] = title.group(1).strip() if title else ""
            row["article_codes"] = ";".join(
                dict.fromkeys(_SELECTOR_LABEL_RE.findall(html))
            )
            image = _OG_IMAGE_RE.search(html)
            if image:
                match = _IMAGE_ARTICLE_RE.search(image.group(1))
                if match:
                    row["image_article"] = match.group(1)
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

    for key, label in [("image_article", "image article id (style+color)"),
                       ("article_codes", "article code set")]:
        groups: dict[str, list[str]] = {}
        missing = 0
        for r in ok:
            value = r[key]
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
    parser.add_argument("site", choices=["fjellsport"])
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--limit", type=int, default=0,
                        help="crawl only the first N product pages (smoke test)")
    args = parser.parse_args()

    session = requests.Session()
    session.headers["User-Agent"] = USER_AGENT

    urls = product_urls(session)
    print(f"product URLs from sitemap: {len(urls)}")
    if args.limit:
        urls = urls[: args.limit]
        print(f"limited to first {len(urls)}")

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        rows = list(pool.map(lambda u: fetch_card(session, u, len(urls)), urls))

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    out = DATA_DIR / f"product_cards_{args.site}.csv"
    with out.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    print(f"wrote {out} ({len(rows)} rows)")

    summarize(rows)

    # Only a run where every single fetch failed is a failed run.
    if rows and all(r["status"] != "ok" for r in rows):
        sys.exit(1)


if __name__ == "__main__":
    main()
