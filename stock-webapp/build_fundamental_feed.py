import csv
import io
import json
import re
from pathlib import Path
from datetime import datetime, timezone, timedelta

import requests
from bs4 import BeautifulSoup

BASE = Path(__file__).resolve().parent
OUT = BASE / "data" / "fundamentals.json"

EQUITY_URL = "https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv"
SME_URL = "https://nsearchives.nseindia.com/emerge/corporates/content/SME_EQUITY_L.csv"
BATCH_SIZE = 80
REQUEST_TIMEOUT = 15
STALE_DAYS = 7

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; RupakFundamentalScanner/2.0)"}


def norm(text):
    return re.sub(r"[^a-z0-9]+", " ", str(text).lower()).strip()


def number(text):
    if text is None:
        return None
    s = str(text).replace(",", "").replace("₹", " ").replace("%", " ").replace("−", "-")
    m = re.search(r"-?\d+(?:\.\d+)?", s)
    return round(float(m.group(0)), 4) if m else None


def load_previous():
    if not OUT.exists():
        return {}
    try:
        return json.loads(OUT.read_text(encoding="utf-8"))
    except Exception:
        return {}


def find_key(row, candidates):
    keys = {norm(k): k for k in row.keys()}
    for c in candidates:
        nc = norm(c)
        if nc in keys:
            return keys[nc]
    for nk, original in keys.items():
        for c in candidates:
            if norm(c) in nk:
                return original
    return None


def fetch_csv(url, segment):
    r = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
    r.raise_for_status()
    text = r.content.decode("utf-8-sig", errors="replace")
    rows = []
    reader = csv.DictReader(io.StringIO(text))
    for raw in reader:
        if not raw:
            continue
        symbol_key = find_key(raw, ["SYMBOL", "Symbol"])
        name_key = find_key(raw, ["NAME OF COMPANY", "Company Name", "NAME"])
        series_key = find_key(raw, ["SERIES", "Series"])
        isin_key = find_key(raw, ["ISIN NUMBER", "ISIN", "ISIN No"])
        listing_key = find_key(raw, ["DATE OF LISTING", "Listing Date"])
        symbol = (raw.get(symbol_key, "") if symbol_key else "").strip()
        if not symbol:
            continue
        series = (raw.get(series_key, "") if series_key else "").strip()
        if segment == "MAIN" and series and series not in {"EQ", "BE", "BZ", "SM", "ST"}:
            continue
        rows.append({
            "symbol": symbol,
            "company": (raw.get(name_key, "") if name_key else symbol).strip() or symbol,
            "series": series or None,
            "isin": (raw.get(isin_key, "") if isin_key else "").strip() or None,
            "listing_date": (raw.get(listing_key, "") if listing_key else "").strip() or None,
            "segment": segment,
        })
    return rows


def fetch_universe():
    items = {}
    errors = []
    for url, segment in [(EQUITY_URL, "MAIN"), (SME_URL, "SME")]:
        try:
            for item in fetch_csv(url, segment):
                items[item["symbol"]] = item
        except Exception as e:
            errors.append({"stage": "universe", "segment": segment, "error": str(e)})
    return items, errors


def screener_metrics(symbol):
    urls = [
        f"https://www.screener.in/company/{symbol}/consolidated/",
        f"https://www.screener.in/company/{symbol}/",
    ]
    html = None
    used_url = None
    last_error = None
    for url in urls:
        try:
            r = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
            if r.ok and ("top-ratios" in r.text or "shareholding" in r.text):
                html = r.text
                used_url = url
                break
            last_error = f"HTTP {r.status_code}"
        except Exception as e:
            last_error = str(e)
    if not html:
        raise RuntimeError(last_error or "Screener page unavailable")

    soup = BeautifulSoup(html, "html.parser")
    metrics = {
        "promoter_holding": None,
        "promoter_change": None,
        "fii_holding": None,
        "dii_holding": None,
        "public_holding": None,
        "market_cap_cr": None,
        "current_price": None,
        "stock_pe": None,
        "book_value": None,
        "dividend_yield": None,
        "roce": None,
        "roe": None,
        "face_value": None,
        "high_52w": None,
        "low_52w": None,
    }

    label_map = {
        "market cap": "market_cap_cr",
        "current price": "current_price",
        "stock p e": "stock_pe",
        "book value": "book_value",
        "dividend yield": "dividend_yield",
        "roce": "roce",
        "roe": "roe",
        "face value": "face_value",
    }

    for li in soup.select("#top-ratios li, .company-ratios li"):
        name_el = li.select_one(".name")
        num_el = li.select_one(".number")
        if not name_el or not num_el:
            continue
        label = norm(name_el.get_text(" ", strip=True))
        key = label_map.get(label)
        if key:
            metrics[key] = number(num_el.get_text(" ", strip=True))
        if label.startswith("high low"):
            nums = re.findall(r"-?\d+(?:,\d{3})*(?:\.\d+)?", num_el.get_text(" ", strip=True))
            if len(nums) >= 2:
                metrics["high_52w"] = number(nums[0])
                metrics["low_52w"] = number(nums[1])

    share = soup.find(id="shareholding")
    if share:
        for table in share.find_all("table"):
            found = False
            for tr in table.find_all("tr"):
                cells = [c.get_text(" ", strip=True) for c in tr.find_all(["th", "td"])]
                if len(cells) < 2:
                    continue
                label = norm(cells[0])
                nums = [number(c) for c in cells[1:]]
                nums = [x for x in nums if x is not None]
                if not nums:
                    continue
                latest = nums[-1]
                if label.startswith("promoter"):
                    metrics["promoter_holding"] = latest
                    if len(nums) >= 2:
                        metrics["promoter_change"] = round(nums[-1] - nums[-2], 4)
                    found = True
                elif label.startswith("fii"):
                    metrics["fii_holding"] = latest
                    found = True
                elif label.startswith("dii"):
                    metrics["dii_holding"] = latest
                    found = True
                elif label.startswith("public"):
                    metrics["public_holding"] = latest
                    found = True
            if found and metrics["promoter_holding"] is not None:
                break

    metrics["source_url"] = used_url
    metrics["fetched_at"] = datetime.now(timezone.utc).isoformat()
    return metrics


def is_stale(metrics):
    if not metrics:
        return True
    ts = metrics.get("fetched_at")
    if not ts:
        return True
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return datetime.now(timezone.utc) - dt > timedelta(days=STALE_DAYS)
    except Exception:
        return True


def main():
    previous = load_previous()
    previous_data = previous.get("data", {}) if isinstance(previous, dict) else {}
    universe, errors = fetch_universe()

    if not universe and previous_data:
        universe = {
            s: {
                "symbol": s,
                "company": (d.get("screen_metrics") or {}).get("company") or s,
                "series": (d.get("screen_metrics") or {}).get("series"),
                "isin": (d.get("screen_metrics") or {}).get("isin"),
                "listing_date": (d.get("screen_metrics") or {}).get("listing_date"),
                "segment": (d.get("screen_metrics") or {}).get("segment") or "UNKNOWN",
            }
            for s, d in previous_data.items()
        }

    data = {}
    for symbol, base in universe.items():
        old = (previous_data.get(symbol) or {}).get("screen_metrics") or {}
        merged = dict(old)
        merged.update({
            "company": base.get("company") or old.get("company") or symbol,
            "series": base.get("series") or old.get("series"),
            "isin": base.get("isin") or old.get("isin"),
            "listing_date": base.get("listing_date") or old.get("listing_date"),
            "segment": base.get("segment") or old.get("segment"),
        })
        data[symbol] = {"screen_metrics": merged}

    candidates = [s for s in sorted(data) if is_stale(data[s]["screen_metrics"])]
    batch = candidates[:BATCH_SIZE]
    updated = 0
    for symbol in batch:
        try:
            fresh = screener_metrics(symbol)
            base = data[symbol]["screen_metrics"]
            fresh.update({
                "company": base.get("company") or symbol,
                "series": base.get("series"),
                "isin": base.get("isin"),
                "listing_date": base.get("listing_date"),
                "segment": base.get("segment"),
            })
            data[symbol] = {"screen_metrics": fresh}
            updated += 1
        except Exception as e:
            errors.append({"symbol": symbol, "stage": "screener", "error": str(e)})

    covered = sum(1 for d in data.values() if (d.get("screen_metrics") or {}).get("fetched_at"))
    payload = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "source": "NSE official equity + SME universe; Screener.in public company pages for ratios",
        "universe_source": [EQUITY_URL, SME_URL],
        "universe_count": len(data),
        "fundamental_coverage": covered,
        "pending_fundamentals": max(0, len(data) - covered),
        "batch_size": BATCH_SIZE,
        "fundamental_filter_fields": [
            "promoter_holding", "promoter_change", "fii_holding", "dii_holding", "public_holding",
            "market_cap_cr", "stock_pe", "dividend_yield", "roce", "roe", "book_value",
            "current_price", "face_value", "high_52w", "low_52w", "series", "segment"
        ],
        "data": data,
        "errors": errors[-200:],
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, indent=2, allow_nan=False), encoding="utf-8")
    print(f"Universe={len(data)} covered={covered} updated_this_run={updated} pending={len(data)-covered}")


if __name__ == "__main__":
    main()
