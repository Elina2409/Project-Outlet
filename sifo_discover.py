"""One-off discovery pass over SIFO's referansebudsjett archive.

Not part of the SKU tracker. Only runs on a GitHub Actions runner (this
sandbox's network policy blocks oslomet.no entirely) to find out how the
archive is actually structured before writing a real parser. Prints
everything it finds; commits nothing.
"""

import re
import sys
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
}

SEED_URLS = [
    "https://www.oslomet.no/om/sifo/referansebudsjettet",
    "https://www.oslomet.no/om/sifo/publikasjoner",
    "https://oda.oslomet.no/oda-xmlui/handle/11250/2740284",
]

YEAR_RE = re.compile(r"20(1[5-9]|2[0-6])")
FILE_RE = re.compile(r"\.(pdf|xlsx?|csv)(\?|$)", re.IGNORECASE)


def fetch(url):
    print(f"\n=== GET {url} ===")
    try:
        resp = requests.get(url, headers=HEADERS, timeout=30)
        print(f"status={resp.status_code} bytes={len(resp.content)}")
        resp.raise_for_status()
        return resp.text
    except Exception as exc:
        print(f"ERROR fetching {url}: {exc}")
        return None


def links_of_interest(html, base_url):
    soup = BeautifulSoup(html, "html.parser")
    hits = []
    for a in soup.find_all("a", href=True):
        href = a["href"]
        text = a.get_text(strip=True)
        haystack = f"{href} {text}"
        if YEAR_RE.search(haystack) or FILE_RE.search(href) or "referansebudsjett" in haystack.lower() or "arkiv" in haystack.lower():
            hits.append((text, urljoin(base_url, href)))
    return hits


def main():
    all_hits = []
    for url in SEED_URLS:
        html = fetch(url)
        if html is None:
            continue
        hits = links_of_interest(html, url)
        print(f"-- {len(hits)} candidate link(s) --")
        for text, href in hits:
            print(f"  [{text!r}] -> {href}")
        all_hits.extend(hits)

    # One level deeper: follow ODA item/collection links found so far.
    oda_links = [href for _, href in all_hits if "oda.oslomet.no" in href and href not in SEED_URLS]
    seen = set(SEED_URLS)
    for href in oda_links[:40]:
        if href in seen:
            continue
        seen.add(href)
        html = fetch(href)
        if html is None:
            continue
        hits = links_of_interest(html, href)
        print(f"-- {len(hits)} candidate link(s) --")
        for text, sub_href in hits:
            print(f"  [{text!r}] -> {sub_href}")


if __name__ == "__main__":
    sys.exit(main())
