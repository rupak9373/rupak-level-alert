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
NEAR_LEVEL_PCT = 1.0
HISTORY_DAYS = 900
CHART_POINTS = 60
TIMEFRAMES = ["15m", "30m", "1H", "2H", "4H", "1D", "1W", "1M"]


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


def distance_pct(price, level):
    if price is None or level is None or level == 0:
        return None
    return clean_number(abs(price - level) / level * 100)


def first_col(df, names):
    for name in names:
        if name in df.columns:
            return name
    return None


def get_history(symbol):
    end = date.today()
    start = end - timedelta(days=HISTORY_DAYS)
    df = stock_df(symbol=symbol, from_date=start, to_date=end, series="EQ")
    if df is None or df.empty:
        raise ValueError("No NSE historical data")

    date_col = first_col(df, ["CH_TIMESTAMP", "TIMESTAMP", "DATE"])
    open_col = first_col(df, ["CH_OPENING_PRICE", "OPEN"])
    high_col = first_col(df, ["CH_TRADE_HIGH_PRICE", "HIGH"])
    low_col = first_col(df, ["CH_TRADE_LOW_PRICE", "LOW"])
    close_col = first_col(df, ["CH_CLOSING_PRICE", "CLOSE"])
    vol_col = first_col(df, ["CH_TOT_TRADED_QTY", "TOTTRDQTY", "VOLUME"])
    required = [date_col, open_col, high_col, low_col, close_col]
    if any(x is None for x in required):
        raise ValueError("NSE OHLC columns unavailable")

    cols = [date_col, open_col, high_col, low_col, close_col]
    if vol_col:
        cols.append(vol_col)
    work = df[cols].copy()
    work[date_col] = pd.to_datetime(work[date_col], errors="coerce", dayfirst=True)
    for c in [open_col, high_col, low_col, close_col]:
        work[c] = pd.to_numeric(work[c], errors="coerce")
    if vol_col:
        work[vol_col] = pd.to_numeric(work[vol_col], errors="coerce")
    else:
        work["_VOL"] = 0
        vol_col = "_VOL"
    work = work.dropna(subset=[date_col, open_col, high_col, low_col, close_col]).sort_values(date_col)
    work = work.rename(columns={date_col:"date", open_col:"open", high_col:"high", low_col:"low", close_col:"close", vol_col:"volume"})
    return work[["date","open","high","low","close","volume"]].reset_index(drop=True)


def get_intraday(symbol, live):
    try:
        raw = live.chart_data(symbol)
        pts = raw.get("grapthData", []) if isinstance(raw, dict) else []
        if not pts:
            return pd.DataFrame(columns=["date","open","high","low","close","volume"])
        rows = []
        for p in pts:
            if not isinstance(p, (list, tuple)) or len(p) < 2:
                continue
            ts = pd.to_datetime(p[0], unit="ms", utc=True, errors="coerce")
            price = clean_number(p[1], 4)
            if pd.isna(ts) or price is None:
                continue
            # NSE timestamps are chart timestamps; convert to India local time for session resampling.
            rows.append({"date": ts.tz_convert("Asia/Kolkata").tz_localize(None), "price": price})
        if not rows:
            return pd.DataFrame(columns=["date","open","high","low","close","volume"])
        ticks = pd.DataFrame(rows).sort_values("date").drop_duplicates("date")
        ticks = ticks.set_index("date")
        one_min = ticks["price"].resample("1min").ohlc().dropna().reset_index()
        one_min["volume"] = 0
        return one_min[["date","open","high","low","close","volume"]]
    except Exception:
        return pd.DataFrame(columns=["date","open","high","low","close","volume"])


def resample_ohlc(df, timeframe):
    if timeframe == "1D":
        return df.copy()
    if timeframe == "1W":
        rule = "W-FRI"
    elif timeframe == "1M":
        rule = "ME"
    else:
        rule = {"15m":"15min", "30m":"30min", "1H":"1h", "2H":"2h", "4H":"4h"}[timeframe]
    work = df.set_index("date").sort_index()
    out = work.resample(rule, origin="start_day", offset="15min").agg({
        "open":"first", "high":"max", "low":"min", "close":"last", "volume":"sum"
    }).dropna(subset=["open","high","low","close"]).reset_index()
    return out


def candle_parts(row):
    o, h, l, c = [float(row[k]) for k in ["open","high","low","close"]]
    body = abs(c-o)
    rng = max(h-l, 1e-9)
    upper = h-max(o,c)
    lower = min(o,c)-l
    return o,h,l,c,body,rng,upper,lower


def detect_patterns(df):
    if df is None or len(df) < 2:
        return []
    cur, prev = df.iloc[-1], df.iloc[-2]
    o,h,l,c,body,rng,upper,lower = candle_parts(cur)
    po,ph,pl,pc,pbody,prng,pupper,plower = candle_parts(prev)
    bullish, bearish = c > o, c < o
    p_bullish, p_bearish = pc > po, pc < po
    body_pct = body / rng
    patterns = []

    if body_pct <= 0.10:
        patterns.append("Doji")
    if bullish and lower >= max(body * 2, rng * 0.45) and upper <= max(body * 0.6, rng * 0.15):
        patterns.append("Hammer")
    if bearish and lower >= max(body * 2, rng * 0.45) and upper <= max(body * 0.6, rng * 0.15):
        patterns.append("Hanging Man")
    if upper >= max(body * 2, rng * 0.45) and lower <= max(body * 0.6, rng * 0.15):
        patterns.append("Inverted Hammer" if bullish else "Shooting Star")
    if p_bearish and bullish and o <= pc and c >= po:
        patterns.append("Bullish Engulfing")
    if p_bullish and bearish and o >= pc and c <= po:
        patterns.append("Bearish Engulfing")
    if p_bearish and bullish and max(o,c) < max(po,pc) and min(o,c) > min(po,pc):
        patterns.append("Bullish Harami")
    if p_bullish and bearish and max(o,c) < max(po,pc) and min(o,c) > min(po,pc):
        patterns.append("Bearish Harami")
    midpoint_prev = (po + pc) / 2
    if p_bearish and bullish and o < pc and c > midpoint_prev and c < po:
        patterns.append("Piercing Line")
    if p_bullish and bearish and o > pc and c < midpoint_prev and c > po:
        patterns.append("Dark Cloud Cover")
    if body_pct >= 0.80 and upper <= rng*0.10 and lower <= rng*0.10:
        patterns.append("Bullish Marubozu" if bullish else "Bearish Marubozu")

    if len(df) >= 3:
        a, b, d = df.iloc[-3], df.iloc[-2], df.iloc[-1]
        ao,ah,al,ac,abody,arng,_,_ = candle_parts(a)
        bo,bh,bl,bc,bbody,brng,_,_ = candle_parts(b)
        do,dh,dl,dc,dbody,drng,_,_ = candle_parts(d)
        if ac < ao and bbody <= abody*0.5 and dc > do and dc >= (ao+ac)/2:
            patterns.append("Morning Star")
        if ac > ao and bbody <= abody*0.5 and dc < do and dc <= (ao+ac)/2:
            patterns.append("Evening Star")
    return list(dict.fromkeys(patterns))


def snap(df, tf):
    if df is None or df.empty:
        return {"patterns": [], "candle": None, "available": False}
    tf_df = resample_ohlc(df, tf)
    if tf_df.empty:
        return {"patterns": [], "candle": None, "available": False}
    row = tf_df.iloc[-1]
    return {
        "patterns": detect_patterns(tf_df),
        "available": len(tf_df) >= 2,
        "candle": {
            "date": row["date"].strftime("%Y-%m-%d %H:%M") if tf not in ["1D","1W","1M"] else row["date"].strftime("%Y-%m-%d"),
            "open": clean_number(row["open"]), "high": clean_number(row["high"]),
            "low": clean_number(row["low"]), "close": clean_number(row["close"]),
        },
    }


def pattern_snapshot(hist, intraday):
    out = {}
    for tf in TIMEFRAMES:
        source = intraday if tf in ["15m","30m","1H","2H","4H"] else hist
        out[tf] = snap(source, tf)
    return out


def key_levels(hist, price):
    levels = {}
    if len(hist) >= 2:
        prev_day = hist.iloc[-2]
        levels["PDH"] = clean_number(prev_day["high"])
        levels["PDL"] = clean_number(prev_day["low"])

    now = pd.Timestamp.now(tz="Asia/Kolkata").tz_localize(None)
    current_year = now.year
    prev_year = current_year - 1
    py = hist[hist["date"].dt.year == prev_year]
    if not py.empty:
        levels["PYH"] = clean_number(py["high"].max())
        levels["PYL"] = clean_number(py["low"].min())

    quarter = ((now.month - 1) // 3) + 1
    q_start_month = 3 * (quarter - 1) + 1
    q_start = pd.Timestamp(current_year, q_start_month, 1)
    qrows = hist[hist["date"] >= q_start]
    if not qrows.empty:
        levels["QO"] = clean_number(qrows.iloc[0]["open"])

    details = {}
    for name, level in levels.items():
        d = distance_pct(price, level)
        details[name] = {"value": level, "distance_pct": d, "near": d is not None and d <= NEAR_LEVEL_PCT}
    return details


def scan_symbol(symbol, live):
    hist = get_history(symbol)
    intraday = get_intraday(symbol, live)
    close = hist["close"].astype(float).reset_index(drop=True)
    vol = hist["volume"].astype(float).reset_index(drop=True)

    quote = live.stock_quote(symbol)
    price_info = quote.get("priceInfo", {}) if isinstance(quote, dict) else {}
    intra = price_info.get("intraDayHighLow", {}) or {}
    price = clean_number(price_info.get("lastPrice")) or clean_number(close.iloc[-1])
    prev = clean_number(price_info.get("previousClose")) or (clean_number(close.iloc[-2]) if len(close) > 1 else price)

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

    d20, d50, d200 = distance_pct(price, sma20), distance_pct(price, sma50), distance_pct(price, sma200)
    tags, score = [], 0
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

    levels = key_levels(hist, price)
    for name, info in levels.items():
        if info.get("near"):
            tags.append(f"Near {name} ({info['distance_pct']}%)")

    chart = [{"date": r["date"].strftime("%Y-%m-%d"), "close": clean_number(r["close"])} for _, r in hist.tail(CHART_POINTS).iterrows()]
    return {
        "symbol": symbol, "price": price, "change_pct": change,
        "open": clean_number(price_info.get("open")), "previous_close": prev,
        "day_high": clean_number(intra.get("max")), "day_low": clean_number(intra.get("min")),
        "vwap": clean_number(price_info.get("vwap")),
        "sma20": sma20, "sma50": sma50, "sma200": sma200,
        "sma20_distance_pct": d20, "sma50_distance_pct": d50, "sma200_distance_pct": d200,
        "near_sma20": d20 is not None and d20 <= NEAR_SMA_PCT,
        "near_sma50": d50 is not None and d50 <= NEAR_SMA_PCT,
        "near_sma200": d200 is not None and d200 <= NEAR_SMA_PCT,
        "rsi14": rsi14, "volume_ratio": volume_ratio, "trend": trend, "score": score,
        "signals": tags, "patterns": pattern_snapshot(hist, intraday), "levels": levels,
        "chart": chart, "source": "NSE India",
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
        "updated_at": datetime.now(timezone.utc).isoformat(), "market": "NSE", "source": "NSE India",
        "count": len(rows), "near_sma_pct": NEAR_SMA_PCT, "near_level_pct": NEAR_LEVEL_PCT,
        "pattern_timeframes": TIMEFRAMES, "key_levels": ["PDH","PDL","QO","PYH","PYL"],
        "results": rows, "errors": errors,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, indent=2, allow_nan=False), encoding="utf-8")
    print(f"Wrote {len(rows)} NSE stocks to {OUT}; errors={len(errors)}")


if __name__ == "__main__":
    main()
