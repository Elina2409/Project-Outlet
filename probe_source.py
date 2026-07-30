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
_LOC_RE = re.compile(r"<loc>([^<]+)</loc>")
_COUNT_KEY_RE = re.compile(
    r'"[^"]*(?:count|hits|antall|numberofproducts|totalproducts)[^"]*"\s*:\s*\d+',
    re.IGNORECASE,
)


def _context(source: str, start: int, end: int, radius: int = 120) -> str:
    snippet = source[max(0, start - radius) : end + radius]
    return " ".join(snippet.split())


def _find_item_lists(node, path: str, out: list, _depth: int = 0) -> None:
    """Recursively find schema.org nodes carrying numberOfItems /
    itemListElement, wherever they're nested (e.g. under "mainEntity")."""
    if _depth > 6:
        return
    if isinstance(node, dict):
        if "numberOfItems" in node or "itemListElement" in node:
            item_list = node.get("itemListElement")
            out.append((path, {
                "@type": node.get("@type"),
                "numberOfItems": node.get("numberOfItems"),
                "itemListElement_len": len(item_list) if isinstance(item_list, list) else None,
            }))
        for key, value in node.items():
            _find_item_lists(value, f"{path}.{key}", out, _depth + 1)
    elif isinstance(node, list):
        for i, value in enumerate(node[:5]):  # cap: don't walk huge arrays
            _find_item_lists(value, f"{path}[{i}]", out, _depth + 1)


def main() -> None:
    if len(sys.argv) < 2:
        sys.exit("usage: python probe_source.py <url> [search-term ...]")
    url = sys.argv[1]
    terms = sys.argv[2:]

    with browser_page() as page:
        seen_responses: list = []

        def on_response(response) -> None:
            ctype = response.headers.get("content-type", "")
            if "json" not in ctype:
                return
            try:
                body = response.text()
            except Exception:
                return
            request = response.request
            seen_responses.append((
                response.url, response.status, body,
                request.method, request.post_data,
            ))

        page.on("response", on_response)

        raw = page.request.get(url).text()
        print(f"=== raw source: {url} ({len(raw)} chars) ===")

        is_html = "<html" in raw[:2000].lower()
        if not is_html:
            # Not an HTML page (sitemap XML, robots.txt, JSON, ...):
            # the document head is the useful part, dump it directly.
            print("\n--- non-HTML document, first 3000 chars ---")
            print(raw[:3000])

        if "<sitemapindex" in raw[:2000] or "<urlset" in raw[:2000]:
            # Break all sitemap URLs down by first path segment - shows
            # at a glance where product pages live and what they look
            # like. For an index, fetch every child sitemap first.
            print("\n--- sitemap: URL breakdown by first path segment ---")
            from collections import Counter

            if "<sitemapindex" in raw[:2000]:
                sources = _LOC_RE.findall(raw)
            else:
                sources = [None]  # the fetched document is the sitemap
            seg_counts: Counter = Counter()
            samples: dict[str, list[str]] = {}
            for child in sources[:20]:  # cap: a runaway index shouldn't hang the probe
                xml = page.request.get(child).text() if child else raw
                locs = _LOC_RE.findall(xml)
                print(f"{child or url}: {len(locs)} URLs")
                for loc in locs:
                    path = re.sub(r"https?://[^/]+", "", loc).split("?")[0]
                    parts = [p for p in path.split("/") if p]
                    seg = "/" + parts[0] if parts else "/"
                    seg_counts[seg] += 1
                    samples.setdefault(seg, []).append(loc)
            print(
                f"total URLs: {sum(seg_counts.values())}, "
                f"distinct first segments: {len(seg_counts)}"
            )
            for seg, n in seg_counts.most_common(30):
                first, last = samples[seg][0], samples[seg][-1]
                print(f"  {seg:<28} {n:>7}")
                print(f"      first: {first}")
                if last != first:
                    print(f"      last : {last}")

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

        nav_texts: list[str] = []
        if is_html:
            # Only render actual pages in the browser - rendering a
            # multi-MB sitemap/XML/robots.txt document as if it were
            # HTML wastes minutes and can OOM the container for
            # nothing (a sitemap has no nav or body text to extract).
            page.goto(url, wait_until="domcontentloaded")
            dismiss_cookie_banner(page)
            page.wait_for_timeout(3000)

            print("\n--- JSON API responses seen while rendering ---")
            # Same idea as the xxl scraper's Apptus interception, but
            # generic: catches whatever API the page itself calls (a
            # products/search/feed endpoint, not just the categories one).
            if not seen_responses:
                print("  (none)")
            for resp_url, status, body, method, post_data in seen_responses[:20]:
                print(f"  {method} {status} {resp_url} ({len(body)} chars)")
                if post_data:
                    print(f"    post_data: {post_data[:500]!r}")
                try:
                    data = json.loads(body)
                except Exception:
                    continue
                if isinstance(data, dict):
                    print(f"    top-level keys: {list(data)[:20]}")
                    hits = data.get("hits")
                    if isinstance(hits, dict):
                        # Elasticsearch-shaped response envelope.
                        inner_hits = hits.get("hits")
                        print(f"    hits.total: {hits.get('total')!r}")
                        if isinstance(inner_hits, list):
                            print(f"    hits.hits length: {len(inner_hits)}")
                            if inner_hits and isinstance(inner_hits[0], dict):
                                print(f"    hits.hits[0] keys: {list(inner_hits[0])[:20]}")
                                source = inner_hits[0].get("_source")
                                if isinstance(source, dict):
                                    print(f"    hits.hits[0]._source keys: {list(source)[:25]}")
                elif isinstance(data, list):
                    print(f"    top-level list, length={len(data)}")
                    if data and isinstance(data[0], dict):
                        print(f"    first item keys: {list(data[0])[:20]}")

            print("\n--- cookies after render (looking for CSRF token) ---")
            cookies = page.context.cookies()
            csrf_cookie = None
            for cookie in cookies:
                flag = ""
                if "xsrf" in cookie["name"].lower() or "csrf" in cookie["name"].lower():
                    csrf_cookie = cookie
                    flag = "  <-- looks like a CSRF token"
                print(f"  {cookie['name']}={cookie['value'][:40]!r}{flag}")

            print("\n--- replaying captured POST requests via plain HTTP ---")
            # Checks whether a POST API call seen during render also works
            # as a direct fetch (no browser/session needed) - the same
            # shortcut sportoutlet's existing /api/v1/categories scraper
            # already relies on. A 419 (Laravel's "CSRF token mismatch")
            # means the cookie alone isn't enough - Laravel's
            # double-submit pattern also needs the same token echoed back
            # as a request header, so retry with that header set from
            # whatever CSRF-looking cookie was found above.
            import urllib.parse

            replayed: set = set()
            for resp_url, _status, _body, method, post_data in seen_responses:
                if method != "POST" or resp_url in replayed:
                    continue
                replayed.add(resp_url)
                header_variants = [{}]
                if csrf_cookie:
                    decoded = urllib.parse.unquote(csrf_cookie["value"])
                    header_variants.append({"X-XSRF-TOKEN": decoded})
                    header_variants.append({"X-CSRF-TOKEN": decoded})
                for extra_headers in header_variants:
                    headers = {"content-type": "application/json", **extra_headers}
                    try:
                        replay = page.request.post(resp_url, data=post_data or "", headers=headers)
                        replay_body = replay.text()
                        label = f"headers={list(extra_headers) or 'none'}"
                        print(f"  POST {resp_url} ({label}) -> {replay.status} ({len(replay_body)} chars)")
                        try:
                            replay_data = json.loads(replay_body)
                            rhits = replay_data.get("hits") if isinstance(replay_data, dict) else None
                            if isinstance(rhits, dict):
                                print(f"    hits.total: {rhits.get('total')!r}")
                        except Exception:
                            pass
                        if replay.status == 200:
                            break
                    except Exception as exc:
                        print(f"  POST {resp_url} ({label}) -> failed: {exc}")

            if csrf_cookie and any(r[0].endswith("/api/v1/articles/search") for r in seen_responses):
                print("\n--- articles/search with an empty filter (whole catalog in one sweep?) ---")
                decoded = urllib.parse.unquote(csrf_cookie["value"])
                try:
                    replay = page.request.post(
                        "https://sportoutlet.no/api/v1/articles/search",
                        data=json.dumps({"query": "", "take": 1, "page": 0, "filters": ""}),
                        headers={"content-type": "application/json", "X-XSRF-TOKEN": decoded},
                    )
                    replay_data = json.loads(replay.text())
                    print(f"  status={replay.status} hits.total={replay_data.get('hits', {}).get('total')!r}")
                except Exception as exc:
                    print(f"  failed: {exc}")

            if csrf_cookie and any(r[0].endswith("/api/v1/articles/search") for r in seen_responses):
                print("\n--- full _source of a known duplicate-name group (colour variant or true dupe?) ---")
                # product_cards.py sportoutlet crawl (2026-07-30) found 92
                # brand+name duplicate groups but the API's article record
                # has no visible Color field - dump every field of every
                # hit for one such group to see what actually differs.
                decoded = urllib.parse.unquote(csrf_cookie["value"])
                for probe_query in terms or ["avery quarter sock 3-pack"]:
                    try:
                        replay = page.request.post(
                            "https://sportoutlet.no/api/v1/articles/search",
                            data=json.dumps({"query": probe_query, "take": 20, "page": 0, "filters": ""}),
                            headers={"content-type": "application/json", "X-XSRF-TOKEN": decoded},
                        )
                        replay_data = json.loads(replay.text())
                        hits = replay_data.get("hits", {}).get("hits", [])
                        print(f"  query={probe_query!r}: {len(hits)} hits")
                        for hit in hits:
                            print(f"    {json.dumps(hit.get('_source', {}), ensure_ascii=False)}")
                    except Exception as exc:
                        print(f"  query={probe_query!r} failed: {exc}")

            print("\n--- all rendered product-tile-ish links (first 25 distinct path shapes) ---")
            # The articles/search API's product records carry no url/slug
            # field, so the product detail page pattern (if any) has to be
            # found in the live DOM instead - broader than the header/nav
            # anchors already dumped below.
            all_links = page.eval_on_selector_all(
                "a[href]",
                "els => els.map(e => e.getAttribute('href'))",
            )
            seen_shapes: dict = {}
            for href in all_links:
                if not href or href.startswith(("http://", "https://", "#", "mailto:", "tel:")):
                    if href and "sportoutlet.no" not in href:
                        continue
                parts = [p for p in href.split("?")[0].split("/") if p]
                shape = f"/{parts[0]}/..." if len(parts) > 1 else href
                seen_shapes.setdefault(shape, href)
            for shape, example in list(seen_shapes.items())[:25]:
                print(f"  {shape}  (e.g. {example})")

            print("\n--- rendered <script id=\"json-ld-*\"> blobs ---")
            # Some frameworks inject JSON-LD client-side after hydration
            # (id="json-ld-items-list" etc.) - it won't be in the raw
            # HTTP source dumped above, only in the live DOM.
            ld_scripts = page.eval_on_selector_all(
                'script[id^="json-ld-"]',
                "els => els.map(e => ({id: e.id, text: e.textContent}))",
            )
            if not ld_scripts:
                print("  (none found)")
            for entry in ld_scripts:
                text = entry["text"] or ""
                print(f"  [{entry['id']}] len={len(text)}")
                try:
                    data = json.loads(text)
                except Exception as exc:
                    print(f"    (unparseable JSON: {exc}) peek: {' '.join(text[:200].split())!r}")
                    continue
                if isinstance(data, dict):
                    print(f"    top-level keys: {list(data)[:20]}")
                else:
                    print(f"    peek: {' '.join(text[:200].split())!r}")
                # numberOfItems/itemListElement are often nested (e.g. under
                # "mainEntity" for a CollectionPage), not top-level - walk
                # the whole structure rather than assume the shape.
                hits: list = []
                _find_item_lists(data, "$", hits)
                for path, info in hits:
                    print(f"    [{path}] @type={info.get('@type')!r} "
                          f"numberOfItems={info.get('numberOfItems')!r} "
                          f"itemListElement_len={info.get('itemListElement_len')!r}")

            print("\n--- rendered nav links (header/nav anchors, first 40) ---")
            anchors = page.eval_on_selector_all(
                "header a[href], nav a[href]",
                "els => els.map(e => ({text: e.innerText.trim().split('\\n')[0], href: e.href}))",
            )
            for anchor in anchors[:40]:
                if anchor["text"]:
                    nav_texts.append(anchor["text"])
                    print(f"  {anchor['text']!r} -> {anchor['href']}")

            print("\n--- 'produkter/varer/artikler' in rendered body text ---")
            body_text = page.inner_text("body")
            hits = 0
            for match in re.finditer(
                r"[\d\u00a0\u202f .]{1,9}\s*(?:produkter|varer|artikler)", body_text, re.IGNORECASE
            ):
                print(f"  {_context(body_text, match.start(), match.end(), 60)!r}")
                hits += 1
                if hits >= 15:
                    break
            if not hits:
                print("  (none)")
        else:
            print("\n--- skipping browser render (non-HTML document) ---")

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
