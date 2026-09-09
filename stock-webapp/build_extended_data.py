import json
import re
from pathlib import Path
from datetime import date, timedelta, datetime, timezone

import pandas as pd
import requests
from bs4 import BeautifulSoup
from jugaad_data.nse import NSELive, stock_df

BASE = Path(__file__).resolve().parent
SYMBOLS_FILE = BASE / "stock_symbols.txt"
OUT = BASE / "data" / "extended.json"
HISTORY_DAYS = 1000
SCREENER_TIMEOUT = 15


def scalar_dict(obj):
    if not isinstance(obj, dict):
        return {}
    out = {}
    for k, v in obj.items():
        if isinstance(v, (str, int, float, bool)) or v is None:
            out[k] = v
    return out


def get_col(df, names):
    for name in names:
        if name in df.columns:
            return name
    return None


def history(symbol):
    end = date.today()
    start = end - timedelta(days=HISTORY_DAYS)
    df = stock_df(symbol=symbol, from_date=start, to_date=end, series="EQ")
    if df is None or df.empty:
        return pd.DataFrame()
    dc = get_col(df, ["CH_TIMESTAMP", "TIMESTAMP", "DATE"])
    oc = get_col(df, ["CH_OPENING_PRICE", "OPEN"])
    hc = get_col(df, ["CH_TRADE_HIGH_PRICE", "HIGH"])
    lc = get_col(df, ["CH_TRADE_LOW_PRICE", "LOW"])
    cc = get_col(df, ["CH_CLOSING_PRICE", "CLOSE"])
    vc = get_col(df, ["CH_TOT_TRADED_QTY", "TOTTRDQTY", "VOLUME"])
    if not all([dc, oc, hc, lc, cc]):
        return pd.DataFrame()
    work = df[[x for x in [dc, oc, hc, lc, cc, vc] if x]].copy()
    work[dc] = pd.to_datetime(work[dc], errors="coerce").dt.tz_localize(None)
    for c in [oc, hc, lc, cc]:
        work[c] = pd.to_numeric(work[c], errors="coerce")
    if vc:
        work[vc] = pd.to_numeric(work[vc], errors="coerce").fillna(0)
    else:
        work["_VOL"] = 0
        vc = "_VOL"
    work = work.dropna(subset=[dc, oc, hc, lc, cc]).sort_values(dc).drop_duplicates(dc, keep="last")
    work = work.rename(columns={dc:"date",oc:"open",hc:"high",lc:"low",cc:"close",vc:"volume"})
    return work[["date","open","high","low","close","volume"]].reset_index(drop=True)


def resample(df, rule):
    if df.empty:
        return df
    return (df.set_index("date").resample(rule).agg({
        "open":"first","high":"max","low":"min","close":"last","volume":"sum"
    }).dropna(subset=["open","high","low","close"]).reset_index())


def rows(df, limit):
    out = []
    for _, r in df.tail(limit).iterrows():
        out.append({
            "date": pd.Timestamp(r["date"]).strftime("%Y-%m-%d"),
            "open": round(float(r["open"]), 2),
            "high": round(float(r["high"]), 2),
            "low": round(float(r["low"]), 2),
            "close": round(float(r["close"]), 2),
            "volume": int(float(r["volume"])) if pd.notna(r["volume"]) else 0,
        })
    return out


def number(text):
    if text is None:
        return None
    s = str(text).replace(",", "").replace("₹", " ").replace("%", " ").replace("−", "-")
    m = re.search(r"-?\d+(?:\.\d+)?", s)
    return round(float(m.group(0)), 4) if m else None


def norm(text):
    return re.sub(r"[^a-z0-9]+", " ", str(text).lower()).strip()


def load_previous():
    if not OUT.exists():
        return {}
    try:
        return json.loads(OUT.read_text(encoding="utf-8"))
    except Exception:
        return {}


def screener_metrics(symbol):
    headers = {"User-Agent": "Mozilla/5.0 (compatible; RupakFundamentalScanner/1.0)"}
    urls = [
        f"https://www.screener.in/company/{symbol}/consolidated/",
        f"https://www.screener.in/company/{symbol}/",
    ]
    html = None
    used_url = None
    last_error = None
    for url in urls:
        try:
            r = requests.get(url, headers=headers, timeout=SCREENER_TIMEOUT)
            if r.ok and "company-info" in r.text:
                html, used_url = r.text, url
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
        if label in ("high low", "high low rs"):
            nums = re.findall(r"-?\d+(?:,\d{3})*(?:\.\d+)?", num_el.get_text(" ", strip=True))
            if len(nums) >= 2:
                metrics["high_52w"] = number(nums[0])
                metrics["low_52w"] = number(nums[1])

    share = soup.find(id="shareholding")
    if share:
        tables = share.find_all("table")
        for table in tables:
            found_any = False
            for tr in table.find_all("tr"):
                cells = [c.get_text(" ", strip=True) for c in tr.find_all(["th", "td"])]
                if len(cells) < 2:
                    continue
                label = norm(cells[0]).replace(" +", "")
                nums = [number(c) for c in cells[1:]]
                nums = [x for x in nums if x is not None]
                if not nums:
                    continue
                latest = nums[-1]
                if label.startswith("promoters") or label.startswith("promoter"):
                    metrics["promoter_holding"] = latest
                    if len(nums) >= 2:
                        metrics["promoter_change"] = round(nums[-1] - nums[-2], 4)
                    found_any = True
                elif label.startswith("fiis") or label.startswith("fii"):
                    metrics["fii_holding"] = latest
                    found_any = True
                elif label.startswith("diis") or label.startswith("dii"):
                    metrics["dii_holding"] = latest
                    found_any = True
                elif label.startswith("public"):
                    metrics["public_holding"] = latest
                    found_any = True
            if found_any and metrics["promoter_holding"] is not None:
                break

    metrics["source_url"] = used_url
    metrics["fetched_at"] = datetime.now(timezone.utc).isoformat()
    return metrics


def merged_screen_metrics(symbol, q, old_item, errors):
    old_screen = (old_item or {}).get("screen_metrics") or {}
    fetched_at = old_screen.get("fetched_at")
    reuse = False
    if fetched_at:
        try:
            reuse = datetime.fromisoformat(fetched_at.replace("Z", "+00:00")).date() == datetime.now(timezone.utc).date()
        except Exception:
            reuse = False
    if reuse and old_screen:
        screen = dict(old_screen)
    else:
        try:
            screen = screener_metrics(symbol)
        except Exception as e:
            screen = dict(old_screen)
            errors.append({"symbol": symbol, "stage": "screener", "error": str(e)})

    metadata = q.get("metadata", {}) if isinstance(q, dict) else {}
    sec = q.get("securityInfo", {}) if isinstance(q, dict) else {}
    ind = q.get("industryInfo", {}) if isinstance(q, dict) else {}
    info = q.get("info", {}) if isinstance(q, dict) else {}
    screen["company"] = info.get("companyName") or info.get("companyNameLong") or screen.get("company") or symbol
    screen["industry"] = ind.get("industry") or ind.get("basicIndustry") or metadata.get("industry") or screen.get("industry")
    screen["sector_pe"] = number(metadata.get("pdSectorPe") or metadata.get("sectorPe"))
    if screen.get("stock_pe") is None:
        screen["stock_pe"] = number(metadata.get("pdSymbolPe") or metadata.get("symbolPe") or metadata.get("pe"))
    screen["is_fno"] = bool(sec.get("isFNOSec") or metadata.get("isFNOSec") or info.get("isFNOSec"))
    screen["isin"] = metadata.get("isin") or metadata.get("isinCode") or info.get("isin")
    screen["listing_date"] = metadata.get("listingDate") or info.get("listingDate")
    return screen


def main():
    symbols = [x.strip().replace(".NS", "") for x in SYMBOLS_FILE.read_text(encoding="utf-8").splitlines() if x.strip() and not x.startswith("#")]
    live = NSELive()
    previous = load_previous()
    previous_data = previous.get("data", {}) if isinstance(previous, dict) else {}
    data, errors = {}, []
    for symbol in symbols:
        try:
            d = history(symbol)
            charts = {
                "1D": rows(d, 120),
                "1W": rows(resample(d, "W-FRI"), 104),
                "1M": rows(resample(d, "ME"), 36),
            }
            try:
                q = live.stock_quote(symbol) or {}
            except Exception as e:
                q = {}
                errors.append({"symbol":symbol,"stage":"quote","error":str(e)})
            fundamentals = {
                "info": scalar_dict(q.get("info")),
                "metadata": scalar_dict(q.get("metadata")),
                "securityInfo": scalar_dict(q.get("securityInfo")),
                "industryInfo": scalar_dict(q.get("industryInfo")),
            }
            screen_metrics = merged_screen_metrics(symbol, q, previous_data.get(symbol, {}), errors)
            data[symbol] = {"charts": charts, "fundamentals": fundamentals, "screen_metrics": screen_metrics}
        except Exception as e:
            errors.append({"symbol":symbol,"stage":"history","error":str(e)})
            if symbol in previous_data:
                data[symbol] = previous_data[symbol]
    payload = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "source": "NSE India + Screener.in public company pages",
        "chart_timeframes": ["1D","1W","1M"],
        "intraday_chart_status": "unavailable_without_reliable_historical_intraday_ohlc",
        "fundamental_filter_fields": [
            "promoter_holding", "promoter_change", "fii_holding", "dii_holding", "public_holding",
            "market_cap_cr", "stock_pe", "sector_pe", "dividend_yield", "roce", "roe", "book_value",
            "current_price", "face_value", "high_52w", "low_52w", "industry", "is_fno"
        ],
        "data": data,
        "errors": errors,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, indent=2, allow_nan=False), encoding="utf-8")
    print(f"Wrote extended data for {len(data)} symbols; errors={len(errors)}")


if __name__ == "__main__":
    main()
