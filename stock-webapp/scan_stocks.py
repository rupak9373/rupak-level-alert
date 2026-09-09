import json
import math
from pathlib import Path
from datetime import date, datetime, timedelta, timezone

import pandas as pd
from jugaad_data.nse import NSELive, stock_df

BASE = Path(__file__).resolve().parent
SYMBOLS_FILE = BASE / "stock_symbols.txt"
OUT = BASE / "data" / "results.json"
NEAR_SMA_PCT = 1.0
HISTORY_DAYS = 760
CHART_POINTS = 60


def clean_number(value, digits=2):
    if value is None:
        return None
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(value):
        return None
    return round(value, digits)


def rsi(series, length=14):
    delta = series.diff()
    gain = delta.clip(lower=0).rolling(length).mean()
    loss = (-delta.clip(upper=0)).rolling(length).mean()
    rs = gain / loss.replace(0, float("nan"))
    return 100 - (100 / (1 + rs))


def sma_distance_pct(price, sma):
    if price is None or sma is None or sma == 0:
        return None
    return clean_number(abs(price - sma) / sma * 100)


def get_history(symbol):
    end = date.today()
    start = end - timedelta(days=HISTORY_DAYS)
    df = stock_df(symbol=symbol, from_date=start, to_date=end, series="EQ")
    if df is None or df.empty:
        raise ValueError("No NSE historical data")

    date_col = "CH_TIMESTAMP" if "CH_TIMESTAMP" in df.columns else "TIMESTAMP"
    close_col = "CH_CLOSING_PRICE" if "CH_CLOSING_PRICE" in df.columns else "CLOSE"
    vol_col = "CH_TOT_TRADED_QTY" if "CH_TOT_TRADED_QTY" in df.columns else "TOTTRDQTY"

    work = df[[date_col, close_col, vol_col]].copy()
    work[date_col] = pd.to_datetime(work[date_col], errors="coerce", dayfirst=True)
    work[close_col] = pd.to_numeric(work[close_col], errors="coerce")
    work[vol_col] = pd.to_numeric(work[vol_col], errors="coerce")
    work = work.dropna(subset=[date_col, close_col]).sort_values(date_col)
    if work.empty:
        raise ValueError("No valid NSE history rows")
    return work, date_col, close_col, vol_col


def scan_symbol(symbol, live):
    hist, date_col, close_col, vol_col = get_history(symbol)
    close = hist[close_col].astype(float).reset_index(drop=True)
    vol = hist[vol_col].astype(float).reset_index(drop=True)

    quote = live.stock_quote(symbol)
    price_info = quote.get("priceInfo", {}) if isinstance(quote, dict) else {}
    intra = price_info.get("intraDayHighLow", {}) or {}

    live_price = clean_number(price_info.get("lastPrice"))
    hist_price = clean_number(close.iloc[-1])
    price = live_price if live_price is not None else hist_price
    prev = clean_number(price_info.get("previousClose"))
    if prev is None:
        prev = clean_number(close.iloc[-2]) if len(close) > 1 else price

    sma20 = clean_number(close.rolling(20).mean().iloc[-1]) if len(close) >= 20 else None
    sma50 = clean_number(close.rolling(50).mean().iloc[-1]) if len(close) >= 50 else None
    sma200 = clean_number(close.rolling(200).mean().iloc[-1]) if len(close) >= 200 else None
    rsi14 = clean_number(rsi(close, 14).iloc[-1], 1) if len(close) >= 15 else None
    avgvol20 = clean_number(vol.rolling(20).mean().iloc[-1]) if len(vol) >= 20 else None
    lastvol = clean_number(vol.iloc[-1])
    volume_ratio = clean_number(lastvol / avgvol20) if lastvol is not None and avgvol20 and avgvol20 > 0 else None

    change = clean_number(price_info.get("pChange"))
    if change is None:
        change = clean_number(((price - prev) / prev * 100) if prev else 0.0)

    d20 = sma_distance_pct(price, sma20)
    d50 = sma_distance_pct(price, sma50)
    d200 = sma_distance_pct(price, sma200)

    tags = []
    score = 0
    if sma20 is not None and price > sma20:
        tags.append("Above SMA20"); score += 1
    if sma20 is not None and sma50 is not None and sma20 > sma50:
        tags.append("SMA20 > SMA50"); score += 1
    if sma200 is not None and price > sma200:
        tags.append("Above SMA200")
    if rsi14 is not None and 50 <= rsi14 <= 70:
        tags.append("RSI Bullish"); score += 1
    if volume_ratio is not None and volume_ratio >= 1.5:
        tags.append("High Volume"); score += 1
    if d20 is not None and d20 <= NEAR_SMA_PCT:
        tags.append(f"Near SMA20 ({d20}%)")
    if d50 is not None and d50 <= NEAR_SMA_PCT:
        tags.append(f"Near SMA50 ({d50}%)")
    if d200 is not None and d200 <= NEAR_SMA_PCT:
        tags.append(f"Near SMA200 ({d200}%)")

    trend = "Bullish" if sma20 is not None and sma50 is not None and price > sma20 > sma50 else (
        "Bearish" if sma20 is not None and sma50 is not None and price < sma20 < sma50 else "Mixed"
    )

    chart = []
    for _, row in hist.tail(CHART_POINTS).iterrows():
        chart.append({
            "date": row[date_col].strftime("%Y-%m-%d"),
            "close": clean_number(row[close_col]),
        })

    return {
        "symbol": symbol,
        "price": price,
        "change_pct": change,
        "open": clean_number(price_info.get("open")),
        "previous_close": prev,
        "day_high": clean_number(intra.get("max")),
        "day_low": clean_number(intra.get("min")),
        "vwap": clean_number(price_info.get("vwap")),
        "sma20": sma20,
        "sma50": sma50,
        "sma200": sma200,
        "sma20_distance_pct": d20,
        "sma50_distance_pct": d50,
        "sma200_distance_pct": d200,
        "near_sma20": d20 is not None and d20 <= NEAR_SMA_PCT,
        "near_sma50": d50 is not None and d50 <= NEAR_SMA_PCT,
        "near_sma200": d200 is not None and d200 <= NEAR_SMA_PCT,
        "rsi14": rsi14,
        "volume_ratio": volume_ratio,
        "trend": trend,
        "score": score,
        "signals": tags,
        "chart": chart,
        "source": "NSE India",
    }


def main():
    symbols = [x.strip().replace(".NS", "") for x in SYMBOLS_FILE.read_text(encoding="utf-8").splitlines() if x.strip() and not x.startswith("#")]
    live = NSELive()
    rows, errors = [], []
    for symbol in symbols:
        try:
            rows.append(scan_symbol(symbol, live))
        except Exception as e:
            errors.append({"symbol": symbol, "error": str(e)})
    rows.sort(key=lambda x: (x["score"], x["change_pct"] if x["change_pct"] is not None else -999999), reverse=True)
    payload = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "market": "NSE",
        "source": "NSE India",
        "count": len(rows),
        "near_sma_pct": NEAR_SMA_PCT,
        "results": rows,
        "errors": errors,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, indent=2, allow_nan=False), encoding="utf-8")
    print(f"Wrote {len(rows)} NSE stocks to {OUT}; errors={len(errors)}")


if __name__ == "__main__":
    main()
