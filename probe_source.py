"""Dump where a page's category/product-count data lives.

Usage: python probe_source.py <url> [search-term ...]

For sites that can't be reached from where Claude runs: run this via the
deploy workflow's probe_url input and read the Cloud Run logs. It
fetches the URL twice - once as raw HTTP (the view-source bytes) and
once rendered in the same headless browser the scrapers use - and
prints where count-like data appears:

- embedded <script> JSON blobs (id/type/size + a peek at each)
- raw-source context around count-keyed JSON fields ("count",
  "productCount", "totalHits", "numberOfProducts", ...)
- the rendered top-nav links (text + href)
- raw-source context around each search term (defaults to the rendered
  nav link texts, i.e. the category names)

Output is plain text on stdout, sized to stay readable in Cloud Run
logs. Nothing here writes to the CSV.
"""
from __future__ import annotations

import json
import re
import sys

from scrapers._common import browser_page, dismiss_cookie_banner

_SCRIPT_RE = re.compile(
    r"<script([^>]*)>(.*?)</script>", re.IGNORECASE | re.DOTALL
)
_COUNT_KEY_RE = re.compile(
    r'"[^"]*(?:count|hits|antall|numberofproducts|totalproducts)[^"]*"\s*:\s*\d+',
    re.IGNORECASE,
)


def _context(source: str, start: int, end: int, radius: int = 120) -> str:
    snippet = source[max(0, start - radius) : end + radius]
    return " ".join(snippet.split())


def main() -> None:
    if len(sys.argv) < 2:
        sys.exit("usage: python probe_source.py <url> [search-term ...]")
    url = sys.argv[1]
    terms = sys.argv[2:]

    with browser_page() as page:
        raw = page.request.get(url).text()
        print(f"=== raw source: {url} ({len(raw)} chars) ===")

        print("\n--- <script> blobs ---")
        for i, match in enumerate(_SCRIPT_RE.finditer(raw)):
            attrs, body = match.group(1).strip(), match.group(2).strip()
            if len(body) < 200:
                continue
            looks_json = body[:1] in "{[" or "json" in attrs.lower()
            print(f"[{i}] attrs={attrs[:100]!r} len={len(body)} json={looks_json}")
            if looks_json:
                try:
                    keys = list(json.loads(body))[:15]
                    print(f"     top-level keys: {keys}")
                except Exception:
                    print(f"     peek: {' '.join(body[:180].split())!r}")

        print("\n--- count-keyed fields in raw source (first 40, deduped) ---")
        seen: set[str] = set()
        shown = 0
        for match in _COUNT_KEY_RE.finditer(raw):
            snippet = _context(raw, match.start(), match.end())
            if snippet in seen:
                continue
            seen.add(snippet)
            print(f"  {snippet}")
            shown += 1
            if shown >= 40:
                break
        if not shown:
            print("  (none)")

        page.goto(url, wait_until="domcontentloaded")
        dismiss_cookie_banner(page)
        page.wait_for_timeout(3000)

        print("\n--- rendered nav links (header/nav anchors, first 40) ---")
        anchors = page.eval_on_selector_all(
            "header a[href], nav a[href]",
            "els => els.map(e => ({text: e.innerText.trim().split('\\n')[0], href: e.href}))",
        )
        nav_texts: list[str] = []
        for anchor in anchors[:40]:
            if anchor["text"]:
                nav_texts.append(anchor["text"])
                print(f"  {anchor['text']!r} -> {anchor['href']}")

        print("\n--- 'produkter/varer/artikler' in rendered body text ---")
        body_text = page.inner_text("body")
        hits = 0
        for match in re.finditer(
            r"[\d   .]{1,9}\s*(?:produkter|varer|artikler)", body_text, re.IGNORECASE
        ):
            print(f"  {_context(body_text, match.start(), match.end(), 60)!r}")
            hits += 1
            if hits >= 15:
                break
        if not hits:
            print("  (none)")

        print("\n--- raw-source context around search terms ---")
        for term in terms or nav_texts[:10]:
            positions = [
                m.start() for m in re.finditer(re.escape(term), raw, re.IGNORECASE)
            ][:3]
            print(f"[{term!r}] {len(positions)} shown:")
            for pos in positions:
                print(f"  {_context(raw, pos, pos + len(term))}")


if __name__ == "__main__":
    main()
