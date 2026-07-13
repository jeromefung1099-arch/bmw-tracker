"""
bmw_hk_tracker.py
=================
Tracks second-hand BMW 530i / 540i / M5 (<=10 years old) in Hong Kong on
HongCars.com, computes a live fair-value baseline, flags GOOD DEALS (priced well
below the market for their model+year), alerts on new listings and price drops,
and pushes Telegram messages. Headless, no browser.

Why HongCars: it serves clean, stateless, UTF-8 URLs with model/mileage/year/price
per listing. (28car was dropped because its search state lives in a browser
session/cookie, so a scheduled request can't drive its filters.)

Pipeline:  scrape HongCars -> filter models/age -> baseline -> detect deals/new/drops
           -> store + diff -> Telegram alert -> CSV export (for the dashboard)

Install:  pip install requests beautifulsoup4
Run:      python bmw_hk_tracker.py
"""

from __future__ import annotations

import csv
import os
import re
import time
import sqlite3
import statistics
import datetime as dt
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

import requests
from bs4 import BeautifulSoup

# --------------------------------------------------------------------------- #
# CONFIG
# --------------------------------------------------------------------------- #

MODELS = ["530i", "540i", "M5"]
MAX_AGE_YEARS = 10
MIN_YEAR = dt.date.today().year - MAX_AGE_YEARS

DEAL_DISCOUNT = 0.12          # flag if >=12% below the model+year median
MIN_COMPARABLES = 4           # ...but only if the baseline has >=4 comparable cars
PRICE_PCT_THRESHOLD = 0.05    # a price DROP this large on a tracked car is notable
PRICE_ABS_THRESHOLD = 20_000
ALERT_NEW_LISTINGS = True     # also ping on any brand-new 530i/540i/M5 (models are
                              # rare enough that you'll want to hear about each one)

HONGCARS = "https://www.hongcars.com"
# Latest-updated BMW list; we page through and filter to our models in code.
SEARCH_PATH = "/en/usedcars/for_sale/BMW_Any-orderby_id"
PAGES = int(os.environ.get("PAGES", "30"))     # ~10 cars/page
REQUEST_DELAY_SEC = 1.5
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; personal-price-tracker/1.0)"}

DB_PATH = Path("listings.db")
CSV_EXPORT = Path("listings_export.csv")

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")


# --------------------------------------------------------------------------- #
# DATA MODEL
# --------------------------------------------------------------------------- #

@dataclass
class Listing:
    source: str
    listing_id: str
    model: str
    year: int
    price: int
    mileage_km: int | None
    url: str
    title: str

    @property
    def key(self) -> str:
        return f"{self.source}:{self.listing_id}"


# --------------------------------------------------------------------------- #
# SCRAPER  (HongCars.com)
# --------------------------------------------------------------------------- #

BMW_MARKERS = ("BMW", "bmw", "寶馬")
MODEL_PATTERNS = {
    "M5":   re.compile(r"\bM5\b"),
    "540i": re.compile(r"\b540\s*i\b", re.I),
    "530i": re.compile(r"\b530\s*i\b", re.I),
}
PRICE_RE = re.compile(r"\$\s*([\d,]{4,})")
YEAR_RE  = re.compile(r"\b(19[89]\d|20[0-4]\d)\b")
VIEW_RE  = re.compile(r"/usedcars/view/(\d+)-")
MILE_RE  = re.compile(r"([\d,]+)\s*km", re.I)


def match_model(text: str) -> str | None:
    for name in ("M5", "540i", "530i"):
        if MODEL_PATTERNS[name].search(text):
            if name == "M5" and not any(m in text for m in BMW_MARKERS):
                continue
            return name
    return None


def parse_listings(html: str) -> list[Listing]:
    """
    Ordered pass over the DOM: each card emits a rich title anchor
    (".../view/ID-... - Automatic - <mileage> - <year> - <fuel>...") followed by
    a "$ price" text node. We pair each title with the next price we encounter.
    """
    soup = BeautifulSoup(html, "html.parser")
    out: list[Listing] = []
    seen: set[str] = set()
    pending = None  # (id, title_text, url)

    for node in soup.descendants:
        name = getattr(node, "name", None)
        if name == "a":
            href = node.get("href", "") or ""
            m = VIEW_RE.search(href)
            if m:
                txt = node.get_text(" ", strip=True)
                if " - " in txt and YEAR_RE.search(txt):
                    url = href if href.startswith("http") else HONGCARS + href
                    pending = (m.group(1), txt, url)
            continue
        if isinstance(node, str) and pending:
            pm = PRICE_RE.search(node)
            if pm:
                vid, txt, url = pending
                pending = None
                if vid in seen:
                    continue
                model = match_model(txt)
                ym = YEAR_RE.search(txt)
                if not (model and ym):
                    continue
                seen.add(vid)
                mile_m = MILE_RE.search(txt)
                mileage = int(mile_m.group(1).replace(",", "")) if mile_m else None
                price = int(pm.group(1).replace(",", ""))
                out.append(Listing("hongcars", vid, model, int(ym.group(1)),
                                   price, mileage, url, txt[:140]))
    return out


def scrape_hongcars() -> list[Listing]:
    found: dict[str, Listing] = {}
    for page in range(1, PAGES + 1):
        path = SEARCH_PATH + (".html" if page == 1 else f"-page_{page}.html")
        try:
            resp = requests.get(HONGCARS + path, headers=HEADERS, timeout=25)
            resp.encoding = "utf-8"
            rows = parse_listings(resp.text)
        except Exception as e:
            print(f"[warn] page {page} failed: {e}")
            break
        for l in rows:
            found[l.key] = l
        print(f"[info] page {page}: {len(rows)} target listings")
        if page < PAGES:
            time.sleep(REQUEST_DELAY_SEC)
    return list(found.values())


# --------------------------------------------------------------------------- #
# FILTER + FAIR VALUE
# --------------------------------------------------------------------------- #

def matches_criteria(l: Listing) -> bool:
    return l.model in MODELS and l.year >= MIN_YEAR and l.price > 10_000


def compute_baselines(listings: list[Listing]) -> dict[tuple[str, int], dict]:
    buckets: dict[tuple[str, int], list[int]] = defaultdict(list)
    for l in listings:
        buckets[(l.model, l.year)].append(l.price)
    return {k: {"median": int(statistics.median(v)), "n": len(v)}
            for k, v in buckets.items()}


def deal_info(l: Listing, baselines) -> dict | None:
    b = baselines.get((l.model, l.year))
    if not b or b["n"] < MIN_COMPARABLES:
        return None
    discount = (b["median"] - l.price) / b["median"]
    if discount >= DEAL_DISCOUNT:
        return {"discount": discount, "median": b["median"], "n": b["n"]}
    return None


# --------------------------------------------------------------------------- #
# STORAGE
# --------------------------------------------------------------------------- #

def init_db() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH)
    con.execute("""CREATE TABLE IF NOT EXISTS listings (
        key TEXT PRIMARY KEY, source TEXT, listing_id TEXT, model TEXT, year INTEGER,
        price INTEGER, mileage_km INTEGER, url TEXT,
        first_seen TEXT, last_seen TEXT, deal_alerted INTEGER DEFAULT 0)""")
    con.execute("""CREATE TABLE IF NOT EXISTS price_history (
        key TEXT, price INTEGER, seen TEXT)""")
    con.commit()
    return con


def load_previous(con) -> dict[str, dict]:
    cur = con.execute("SELECT key, price, deal_alerted FROM listings")
    return {r[0]: {"price": r[1], "deal_alerted": r[2]} for r in cur.fetchall()}


def upsert(con, l: Listing, now: str, deal_alerted: int) -> None:
    con.execute("""INSERT INTO listings
        (key, source, listing_id, model, year, price, mileage_km, url,
         first_seen, last_seen, deal_alerted)
        VALUES (?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(key) DO UPDATE SET price=excluded.price, last_seen=excluded.last_seen,
            deal_alerted=MAX(listings.deal_alerted, excluded.deal_alerted)""",
        (l.key, l.source, l.listing_id, l.model, l.year, l.price, l.mileage_km,
         l.url, now, now, deal_alerted))
    con.execute("INSERT INTO price_history (key, price, seen) VALUES (?,?,?)",
                (l.key, l.price, now))


# --------------------------------------------------------------------------- #
# CHANGE DETECTION + ALERTING
# --------------------------------------------------------------------------- #

def is_significant(old: int, new: int) -> bool:
    d = abs(new - old)
    return d >= PRICE_ABS_THRESHOLD or (old and d / old >= PRICE_PCT_THRESHOLD)


def send_telegram(message: str) -> None:
    if not (TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID):
        print("[alert] Telegram not configured; printing instead:\n" + message)
        return
    requests.post(
        f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
        json={"chat_id": TELEGRAM_CHAT_ID, "text": message,
              "parse_mode": "HTML", "disable_web_page_preview": True},
        timeout=20)


def km(l: Listing) -> str:
    return f"{l.mileage_km:,}km" if l.mileage_km else "km n/a"


def build_alert(deals, new_ones, drops) -> str | None:
    if not (deals or new_ones or drops):
        return None
    L = [f"<b>🚗 BMW HK tracker — {dt.date.today():%Y-%m-%d}</b>"]
    if deals:
        L.append(f"\n<b>🔥 {len(deals)} GOOD DEAL(S)</b>")
        for l, d in deals:
            L.append(f"🔥 <b>{l.year} BMW {l.model} — HK${l.price:,}</b> ({km(l)})\n"
                     f"   {d['discount']*100:.0f}% below HK${d['median']:,} median "
                     f"for {l.year} {l.model} ({d['n']} comps)\n   {l.url}")
    if new_ones:
        L.append(f"\n<b>🆕 {len(new_ones)} new listing(s)</b>")
        for l in new_ones:
            L.append(f"🆕 {l.year} {l.model} — HK${l.price:,} ({km(l)})\n   {l.url}")
    if drops:
        L.append(f"\n<b>🔻 {len(drops)} price drop(s)</b>")
        for l, old in drops:
            L.append(f"🔻 {l.year} {l.model}: HK${old:,} → HK${l.price:,}\n   {l.url}")
    return "\n".join(L)


# --------------------------------------------------------------------------- #
# EXPORT + MAIN
# --------------------------------------------------------------------------- #

def export_csv(con) -> None:
    cur = con.execute("SELECT * FROM listings ORDER BY model, year DESC, price")
    cols = [d[0] for d in cur.description]
    with CSV_EXPORT.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f); w.writerow(cols); w.writerows(cur.fetchall())


ENABLE_28CAR = os.environ.get("ENABLE_28CAR", "true").lower() in ("1", "true", "yes")


def scrape_28car_source() -> list[Listing]:
    """Optional second source: 28car via Playwright (see scrape_28car.py)."""
    if not ENABLE_28CAR:
        return []
    try:
        import scrape_28car
        return [Listing(**d) for d in scrape_28car.scrape()]
    except Exception as e:
        print(f"[warn] 28car source unavailable: {e}")
        return []


def main() -> None:
    now = dt.datetime.now().isoformat(timespec="seconds")
    con = init_db()
    previous = load_previous(con)

    scraped = scrape_hongcars() + scrape_28car_source()
    current = [l for l in scraped if matches_criteria(l)]
    print(f"[info] {len(current)} matching listings (models={MODELS}, year>={MIN_YEAR})")

    baselines = compute_baselines(current)
    first_run = len(previous) == 0     # empty DB: seed silently, don't flood as "new"
    deals, new_ones, drops = [], [], []

    for l in current:
        prev = previous.get(l.key)
        di = deal_info(l, baselines)
        is_new = prev is None
        fresh_deal = di and (is_new or prev["deal_alerted"] == 0)
        if fresh_deal:
            deals.append((l, di))
        elif is_new and ALERT_NEW_LISTINGS and not first_run:
            new_ones.append(l)                       # new, but not a statistical deal
        if prev and is_significant(prev["price"], l.price) and l.price < prev["price"]:
            drops.append((l, prev["price"]))
        upsert(con, l, now, deal_alerted=1 if (di or (prev and prev["deal_alerted"])) else 0)

    con.commit()
    if (msg := build_alert(deals, new_ones, drops)):
        send_telegram(msg)
    else:
        print("[info] nothing new worth alerting.")
    export_csv(con)
    con.close()


if __name__ == "__main__":
    main()
