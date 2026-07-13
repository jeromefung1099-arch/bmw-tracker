"""
scrape_28car.py
===============
Playwright-based scraper for 28car.com (which requires a real browser session:
its search filters live in cookies/form POSTs, not URLs).

Flow per model query ("530", "540", "M5"):
  open private-seller list -> type query in the model (型號) search box ->
  click 搜尋 -> parse result rows -> click 下頁 (next page) up to PAGES_28CAR.

Returns plain dicts; bmw_hk_tracker.py converts them to Listing objects.
Self-contained on purpose (no import of the tracker) to avoid circular imports.

Requires:
    pip install playwright
    playwright install chromium
Enable in the tracker with env ENABLE_28CAR=true.
"""

from __future__ import annotations

import os
import re
import time

BASE = "https://www.28car.com/"
LIST_URL = BASE + "sell_lst.php"

QUERIES = ("530", "540", "M5")
PAGES_28CAR = int(os.environ.get("PAGES_28CAR", "3"))
NAV_TIMEOUT_MS = 45_000
POLITE_PAUSE_SEC = 2.5

# --- row-text parsing (offline-testable, no browser needed) ------------------ #

BMW_MARKERS = ("寶馬", "BMW", "bmw")
MODEL_PATTERNS = {
    "M5":   re.compile(r"\bM5\b"),
    "540i": re.compile(r"\b540\s*i\b", re.I),
    "530i": re.compile(r"\b530\s*i\b", re.I),
}
PRICE_RE = re.compile(r"\$\s*([\d,]{4,})")
YEAR_RE  = re.compile(r"\b(19[89]\d|20[0-4]\d)\b")
PHONE_RE = re.compile(r"(\d{8})")
VID_RE   = re.compile(r"h_vid=(\d+)")


def match_model(text: str) -> str | None:
    stripped = PRICE_RE.sub(" ", text)      # so "$540,000" can't read as 540i
    for name in ("M5", "540i", "530i"):
        if MODEL_PATTERNS[name].search(stripped):
            if name == "M5" and not any(m in text for m in BMW_MARKERS):
                continue
            return name
    return None


def parse_row(text: str, hrefs: list[str]) -> dict | None:
    """Turn one result row's visible text + its links into a listing dict."""
    if "$" not in text:
        return None
    model = match_model(text)
    price_m, year_m = PRICE_RE.search(text), YEAR_RE.search(text)
    if not (model and price_m and year_m):
        return None
    vid, url = None, LIST_URL
    for h in hrefs:
        m = VID_RE.search(h)
        if m:
            vid = m.group(1)
            url = h if h.startswith("http") else BASE + h.lstrip("/")
            break
    phone_m = PHONE_RE.search(text)
    if vid is None:
        # stable-ish fallback key so re-runs don't duplicate
        vid = f"{model}-{year_m.group(1)}-{(phone_m.group(1) if phone_m else 'nophone')}"
    return {
        "source": "28car",
        "listing_id": vid,
        "model": model,
        "year": int(year_m.group(1)),
        "price": int(price_m.group(1).replace(",", "")),
        "mileage_km": None,                 # 28car list rows don't show mileage
        "url": url,
        "title": text[:140],
    }


# --- browser automation ------------------------------------------------------ #

def _search_and_collect(page, query: str) -> list[dict]:
    out: list[dict] = []
    page.goto(LIST_URL, timeout=NAV_TIMEOUT_MS, wait_until="domcontentloaded")
    page.wait_for_timeout(1500)

    # The model search box sits beside the 型號 dropdown, next to the 搜尋 button.
    # Try several strategies; log which one worked.
    box = None
    for how, sel in [
        ("input near 搜尋",  "xpath=//input[@type='text'][following::*[contains(text(),'搜尋')]]"),
        ("last text input",  "input[type='text']"),
    ]:
        try:
            loc = page.locator(sel)
            if loc.count() > 0:
                box = loc.nth(loc.count() - 1)
                print(f"[28car] using search box strategy: {how}")
                break
        except Exception:
            continue
    if box is None:
        print("[28car][warn] couldn't find the model search box; page layout changed?")
        return out

    box.fill(query)
    clicked = False
    for sel in ["text=搜尋", "input[value='搜尋']", "button:has-text('搜尋')"]:
        try:
            page.locator(sel).first.click(timeout=5_000)
            clicked = True
            break
        except Exception:
            continue
    if not clicked:
        box.press("Enter")
    page.wait_for_timeout(2500)

    for pg in range(1, PAGES_28CAR + 1):
        rows = page.locator("tr")
        n = rows.count()
        got = 0
        for i in range(n):
            row = rows.nth(i)
            try:
                text = row.inner_text(timeout=2_000).replace("\n", " ").strip()
            except Exception:
                continue
            hrefs = []
            try:
                links = row.locator("a")
                for j in range(min(links.count(), 6)):
                    h = links.nth(j).get_attribute("href")
                    if h:
                        hrefs.append(h)
            except Exception:
                pass
            item = parse_row(text, hrefs)
            if item:
                out.append(item)
                got += 1
        print(f"[28car] '{query}' page {pg}: {got} target rows")

        if pg == PAGES_28CAR:
            break
        try:
            page.locator("text=下頁").first.click(timeout=5_000)
            page.wait_for_timeout(2500)
        except Exception:
            print(f"[28car] no next page after page {pg}")
            break
    return out


def scrape() -> list[dict]:
    """Entry point used by bmw_hk_tracker. Returns [] on any failure."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("[28car][warn] playwright not installed; skipping 28car "
              "(pip install playwright && playwright install chromium)")
        return []

    found: dict[str, dict] = {}
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            ctx = browser.new_context(
                user_agent=("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                            "AppleWebKit/537.36 (KHTML, like Gecko) "
                            "Chrome/125.0 Safari/537.36"),
                locale="zh-HK",
            )
            page = ctx.new_page()
            for q in QUERIES:
                try:
                    for item in _search_and_collect(page, q):
                        found[f"{item['source']}:{item['listing_id']}"] = item
                except Exception as e:
                    print(f"[28car][warn] query '{q}' failed: {e}")
                time.sleep(POLITE_PAUSE_SEC)
            browser.close()
    except Exception as e:
        print(f"[28car][warn] browser session failed entirely: {e}")
        return []
    print(f"[28car] total unique target listings: {len(found)}")
    return list(found.values())


if __name__ == "__main__":
    for it in scrape():
        print(it)
