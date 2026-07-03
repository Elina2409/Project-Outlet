"""Diagnose why a scraper finds no product tiles on a page.

Usage: python debug_page.py [url]   (default: the Sport Outlet catalog)

Loads the URL in the same headless browser the scrapers use, then prints
what actually rendered: final URL, title, image/link tallies, sample img
alt/src values, and candidate consent buttons. Saves a full-page
screenshot and the rendered HTML next to this script for inspection.
"""
from __future__ import annotations

import sys

from scrapers._common import browser_page, dismiss_cookie_banner
from scrapers.sportoutlet import CATEGORY_NAMES

DEFAULT_URL = "https://sportoutlet.no/campaign"

_FIND_TEXT_ELEMENTS_JS = """(name) => {
  const out = [];
  for (const el of document.querySelectorAll('*')) {
    if (el.children.length === 0 && el.textContent.trim() === name) {
      const a = el.closest('a');
      out.push({
        tag: el.tagName,
        cls: (el.className || '').toString().slice(0, 60),
        anchorHref: a ? a.href : null,
        parentTag: el.parentElement ? el.parentElement.tagName : null,
      });
    }
  }
  return out.slice(0, 5);
}"""


def _labeled_counts(node, out: list) -> None:
    if isinstance(node, dict):
        if isinstance(node.get("label"), str) and isinstance(node.get("count"), int):
            out.append((node["label"], node["count"], bool(node.get("selected"))))
        for value in node.values():
            _labeled_counts(value, out)
    elif isinstance(node, list):
        for value in node:
            _labeled_counts(value, out)


def main() -> None:
    url = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_URL
    api_payloads: list = []
    with browser_page() as page:
        def on_response(response) -> None:
            if "apptus.cloud" not in response.url:
                return
            try:
                api_payloads.append((response.url, response.json()))
            except Exception:
                pass

        page.on("response", on_response)
        page.goto(url, wait_until="domcontentloaded")
        page.wait_for_timeout(5000)

        print(f"requested url : {url}")
        print(f"final url     : {page.url}")
        print(f"title         : {page.title()}")

        buttons = page.eval_on_selector_all(
            "button", "els => els.map(e => e.innerText.trim()).filter(t => t).slice(0, 20)"
        )
        print(f"button texts  : {buttons}")

        dismiss_cookie_banner(page)
        page.wait_for_timeout(3000)

        imgs = page.eval_on_selector_all(
            "img", "els => els.map(e => ({alt: e.getAttribute('alt'), src: e.getAttribute('src')}))"
        )
        links = page.eval_on_selector_all("a[href]", "els => els.map(e => e.href)")
        print(f"img count     : {len(imgs)}")
        print(f"link count    : {len(links)}")
        print("first 15 imgs :")
        for img in imgs[:15]:
            print(f"  alt={img['alt']!r} src={str(img['src'])[:100]!r}")
        product_imgs = [i for i in imgs if (i["alt"] or "").startswith("Image of product:")]
        print(f"imgs matching 'Image of product:' prefix: {len(product_imgs)}")
        print("first 15 links:")
        for href in links[:15]:
            print(f"  {href}")

        # How does each category name appear in the DOM? (helps decide
        # between anchor-matching and click-through discovery)
        try:
            toggle = page.get_by_text("Kategori", exact=True).first
            if toggle.is_visible(timeout=2000):
                toggle.click(timeout=2000)
                page.wait_for_timeout(1500)
        except Exception as exc:
            print(f"could not open Kategori panel: {exc}")
        print("category name elements (after opening Kategori):")
        for name in CATEGORY_NAMES:
            els = page.evaluate(_FIND_TEXT_ELEMENTS_JS, name)
            print(f"  {name!r}: {els}")

        body_text = page.inner_text("body")
        idx = body_text.find("Artikler")
        context = body_text[max(0, idx - 60) : idx + 60].replace("\n", " | ") if idx != -1 else ""
        print(f"'Artikler' in body text: {idx != -1}  context: {context!r}")

        print(f"apptus API responses seen: {len(api_payloads)}")
        for api_url, payload in api_payloads[:5]:
            pairs: list = []
            _labeled_counts(payload, pairs)
            print(f"  {api_url.split('?')[0]}")
            print(f"    label/count pairs (selected*): "
                  f"{[(l + ('*' if s else ''), c) for l, c, s in pairs[:25]]}")

        try:
            response = page.request.get("https://sportoutlet.no/api/v1/categories")
            print(f"categories api status: {response.status}")
            print(f"categories api body (first 4000 chars):\n{response.text()[:4000]}")
        except Exception as exc:
            print(f"categories api fetch failed: {exc}")

        page.screenshot(path="debug_screenshot.png", full_page=False)
        with open("debug_page.html", "w", encoding="utf-8") as f:
            f.write(page.content())
        print("saved debug_screenshot.png and debug_page.html")


if __name__ == "__main__":
    main()
