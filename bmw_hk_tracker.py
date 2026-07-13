"""
bmw_hk_tracker.py
=================
Tracks second-hand BMW 530i / 540i / M5 listings in Hong Kong on 28car.com,
computes a live fair-value baseline, flags GOOD DEALS (priced well below the
market for their model+year), and pushes Telegram alerts. Runs headless — no
browser needed, because 28car is server-rendered HTML (Big5-encoded).

Pipeline:  scrape 28car -> filter models/age -> baseline -> detect deals & drops
           -> store + diff -> Telegram alert -> CSV export (for the dashboard)

Install:
    pip install requests beautifulsoup4
Run:
    python bmw_hk_tracker.py

Credentials + search URL can come from env vars (used by GitHub Actions) or the
CONFIG block below.
"""

from __future__ import annotations

import csv
import os
import re
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

MODELS = ["530i", "540i", "M5"]            # models to track (matched case-insensitively)
MAX_AGE_YEARS = 10
MIN_YEAR = dt.date.today().year - MAX_AGE_YEARS

# "Good deal" definition -----------------------------------------------------
DEAL_DISCOUNT = 0.12        # flag if >=12% below the model+year median
MIN_COMPARABLES = 4         # ...but only if the baseline has >=4 comparable cars
# Significant price move on an already-tracked listing:
PRICE_PCT_THRESHOLD = 0.05  # 5%
PRICE_ABS_THRESHOLD = 20_000  # or HK$20,000

# 28car search URLs to scan. Paste your BMW-filtered search URL(s) here:
#   1. Go to https://www.28car.com , open the private-seller search (私人售車)
#   2. Filter brand = BMW (寶馬); optionally set year >= 2016
#   3. Copy the resulting URL from the address bar and paste it below.
# You can add several (e.g. private + dealer searches). The default below is the
# unfiltered list, which still works (models are filtered in code) but is slower.
SEARCH_URLS = [
    u.strip() for u in os.environ.get(
        "SEARCH_URLS",
        "https://www.28car.com/sell_lst.php"
    ).split(",") if u.strip()
]
PAGES_PER_URL = int(os.environ.get("PAGES_PER_URL", "3"))  # how many result pages to walk

BASE = "https://www.28car.com/"
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; personal-price-tracker/1.0)"}
REQUEST_DELAY_SEC = 3       # be polite: pause between requests

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
    owners: int | None
    seller_phone: str
    url: str
    raw: str            # the row's raw text, for the description/mileage

    @property
    def key(self) -> str:
        return f"{self.source}:{self.listing_id}"


# --------------------------------------------------------------------------- #
# SCRAPER  (28car.com)
# --------------------------------------------------------------------------- #

MODEL_PATTERNS = {
    "M5":   re.compile(r"\bM5\b", re.I),
    "540i": re.compile(r"\b540\s*i?\b", re.I),
    "530i": re.compile(r"\b530\s*i?\b", re.I),
}
PRICE_RE = re.compile(r"\$\s*([\d,]{4,})")
YEAR_RE  = re.compile(r"\b(19[89]\d|20[0-4]\d)\b")   # a bare 4-digit model year
PHONE_RE = re.compile(r"(\d{8})")                     # HK phone in the seller line
VHCID_RE = re.compile(r"h_vhc_id=(\w+)")


def fetch_page(url: str, page: int) -> str:
    """Fetch one 28car results page and decode Big5 -> str."""
    params = {"h_page": page} if page > 1 else {}
    resp = requests.get(url, params=params, headers=HEADERS, timeout=25)
    resp.encoding = "big5"          # 28car serves Big5, not UTF-8
    return resp.text


def match_model(text: str) -> str | None:
    # Check M5 first so "M5" isn't shadowed; then 540/530.
    for name in ("M5", "540i", "530i"):
        if MODEL_PATTERNS[name].search(text):
            return name
    return None


def parse_listings(html: str) -> list[Listing]:
    """
    28car lists each car as a table row. We detect listing rows heuristically
    (a row that contains a $price AND a 4-digit year AND one of our models),
    then pull fields by regex so we're resilient to minor markup changes.
    """
    soup = BeautifulSoup(html, "html.parser")
    out: list[Listing] = []

    for tr in soup.find_all("tr"):
        text = tr.get_text(" ", strip=True)
        if "$" not in text:
            continue
        price_m, year_m = PRICE_RE.search(text), YEAR_RE.search(text)
        if not (price_m and year_m):
            continue
        # Match the model on text with prices removed, so "$540,000" can't be
        # misread as a 540i (the comma otherwise creates a word boundary).
        model = match_model(PRICE_RE.sub(" ", text))
        if not model:
            continue

        price = int(price_m.group(1).replace(",", ""))
        year = int(year_m.group(1))
        phone_m = PHONE_RE.search(text)
        phone = phone_m.group(1) if phone_m else ""

        # detail link + stable id
        url, vid = url_and_id(tr, model, year, price, phone)

        # owners: the small integer sitting right after the price, if present
        owners = None
        tail = text[price_m.end():]
        owners_m = re.search(r"\b(\d{1,2})\b", tail)
        if owners_m:
            owners = int(owners_m.group(1))

        out.append(Listing("28car", vid, model, year, price, owners, phone, url, text))

    return out


def url_and_id(tr, model, year, price, phone) -> tuple[str, str]:
    for a in tr.find_all("a", href=True):
        m = VHCID_RE.search(a["href"])
        if m:
            href = a["href"]
            full = href if href.startswith("http") else BASE + href.lstrip("/")
            return full, m.group(1)
    # Fallback synthetic key: stable across price changes (same seller+car).
    synthetic = f"{model}-{year}-{phone or 'nophone'}"
    return SEARCH_URLS[0], synthetic


def scrape_28car() -> list[Listing]:
    import time
    seen: dict[str, Listing] = {}
    for url in SEARCH_URLS:
        for page in range(1, PAGES_PER_URL + 1):
            try:
                html = fetch_page(url, page)
            except Exception as e:
                print(f"[warn] fetch failed {url} p{page}: {e}")
                break
            rows = parse_listings(html)
            for l in rows:
                seen[l.key] = l        # dedupe across pages/urls
            print(f"[info] {url} page {page}: {len(rows)} matching rows")
            time.sleep(REQUEST_DELAY_SEC)
    return list(seen.values())


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

import sqlite3


def init_db() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH)
    con.execute("""CREATE TABLE IF NOT EXISTS listings (
        key TEXT PRIMARY KEY, source TEXT, listing_id TEXT, model TEXT, year INTEGER,
        price INTEGER, owners INTEGER, seller_phone TEXT, url TEXT,
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
        (key, source, listing_id, model, year, price, owners, seller_phone, url,
         first_seen, last_seen, deal_alerted)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(key) DO UPDATE SET price=excluded.price, last_seen=excluded.last_seen,
            deal_alerted=MAX(listings.deal_alerted, excluded.deal_alerted)""",
        (l.key, l.source, l.listing_id, l.model, l.year, l.price, l.owners,
         l.seller_phone, l.url, now, now, deal_alerted))
    con.execute("INSERT INTO price_history (key, price, seen) VALUES (?,?,?)",
                (l.key, l.price, now))


# --------------------------------------------------------------------------- #
# CHANGE DETECTION
# --------------------------------------------------------------------------- #

def is_significant(old: int, new: int) -> bool:
    d = abs(new - old)
    return d >= PRICE_ABS_THRESHOLD or (old and d / old >= PRICE_PCT_THRESHOLD)


# --------------------------------------------------------------------------- #
# ALERTING
# --------------------------------------------------------------------------- #

def send_telegram(message: str) -> None:
    if not (TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID):
        print("[alert] Telegram not configured; printing instead:\n" + message)
        return
    requests.post(
        f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
        json={"chat_id": TELEGRAM_CHAT_ID, "text": message,
              "parse_mode": "HTML", "disable_web_page_preview": True},
        timeout=20)


def fmt_deal(l: Listing, d: dict) -> str:
    return (f"🔥 <b>{l.year} BMW {l.model} — HK${l.price:,}</b>\n"
            f"   {d['discount']*100:.0f}% below the HK${d['median']:,} median "
            f"for {l.year} {l.model} ({d['n']} comps)\n   {l.url}")


def build_alert(deals, drops) -> str | None:
    if not (deals or drops):
        return None
    lines = [f"<b>🚗 BMW HK tracker — {dt.date.today():%Y-%m-%d}</b>"]
    if deals:
        lines.append(f"\n<b>{len(deals)} GOOD DEAL(S):</b>")
        lines += [fmt_deal(l, d) for l, d in deals]
    if drops:
        lines.append(f"\n<b>{len(drops)} price drop(s) on tracked cars:</b>")
        for l, old in drops:
            lines.append(f"🔻 {l.year} {l.model}: HK${old:,} → HK${l.price:,}\n   {l.url}")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# EXPORT
# --------------------------------------------------------------------------- #

def export_csv(con) -> None:
    cur = con.execute("SELECT * FROM listings ORDER BY model, year DESC, price")
    cols = [d[0] for d in cur.description]
    with CSV_EXPORT.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(cols)
        w.writerows(cur.fetchall())


# --------------------------------------------------------------------------- #
# MAIN
# --------------------------------------------------------------------------- #

def main() -> None:
    now = dt.datetime.now().isoformat(timespec="seconds")
    con = init_db()
    previous = load_previous(con)

    scraped = scrape_28car()
    current = [l for l in scraped if matches_criteria(l)]
    print(f"[info] {len(current)} matching listings "
          f"(models={MODELS}, year>={MIN_YEAR})")

    baselines = compute_baselines(current)

    deals, drops = [], []
    for l in current:
        prev = previous.get(l.key)
        di = deal_info(l, baselines)
        # A *fresh* good deal = qualifies now and hasn't been alerted before.
        fresh_deal = di and (prev is None or prev["deal_alerted"] == 0)
        if fresh_deal:
            deals.append((l, di))
        # Significant price move on a car we already knew about.
        if prev and is_significant(prev["price"], l.price) and l.price < prev["price"]:
            drops.append((l, prev["price"]))
        upsert(con, l, now, deal_alerted=1 if (di or (prev and prev["deal_alerted"])) else 0)

    con.commit()

    if (msg := build_alert(deals, drops)):
        send_telegram(msg)
    else:
        print("[info] nothing new worth alerting.")

    export_csv(con)
    con.close()


if __name__ == "__main__":
    main()
