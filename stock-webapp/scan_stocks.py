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
INTRADAY_TFS = {"15m", "30m", "1H", "2H", "4H"}
PATTERN_PRIORITY = [
    "Morning Star", "Evening Star", "Bullish Engulfing", "Bearish Engulfing",
    "Piercing Line", "Dark Cloud Cover", "Hammer", "Hanging Man",
    "Inverted Hammer", "Shooting Star", "Bullish Harami", "Bearish Harami",
    "Bullish Marubozu", "Bearish Marubozu", "Doji",
]


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


def first_col(df, names):
    for name in names:
        if name in df.columns:
            return name
    return None


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


def normalize_nse_dates(series):
    raw = series.astype(str).str.strip()
    has_time = raw.str.contains(r"T|Z|\+\d\d:?\d\d", regex=True).mean() > 0.25
    if has_time:
        parsed = pd.to_datetime(raw, errors="coerce", utc=True)
        dates = parsed.dt.tz_convert("Asia/Kolkata").dt.tz_localize(None).dt.normalize()
    else:
        dates = pd.to_datetime(raw, errors="coerce", dayfirst=False).dt.normalize()

    # Some NSE responses expose the UTC calendar day (one day before the NSE session).
    # Pick the +/-1 day alignment that produces the fewest weekend rows.
    valid = dates.dropna()
    if len(valid) >= 10:
        candidates = {shift: (valid + pd.Timedelta(days=shift)).dt.dayofweek.ge(5).sum() for shift in (-1, 0, 1)}
        best = min(candidates, key=candidates.get)
        if candidates[best] < candidates[0]:
            dates = dates + pd.Timedelta(days=best)
    return dates


def adjust_corporate_actions(df):
    """Back-adjust old OHLC around obvious split/bonus discontinuities."""
    work = df.copy().reset_index(drop=True)
    adjustments = []
    ratios = [0.1, 0.2, 0.25, 1/3, 0.5, 2.0, 3.0, 4.0, 5.0, 10.0]
    for i in range(1, len(work)):
        prev = float(work.at[i - 1, "close"])
        cur = float(work.at[i, "close"])
        if prev <= 0 or cur <= 0:
            continue
        raw_ratio = cur / prev
        if 0.65 <= raw_ratio <= 1.55:
            continue
        factor = min(ratios, key=lambda x: abs(math.log(raw_ratio / x)))
        error = abs(raw_ratio - factor) / factor
        if error <= 0.12:
            cols = ["open", "high", "low", "close"]
            work.loc[:i - 1, cols] = work.loc[:i - 1, cols] * factor
            adjustments.append({
                "date": work.at[i, "date"].strftime("%Y-%m-%d"),
                "factor": round(factor, 6),
            })
    return work, adjustments


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
    work[date_col] = normalize_nse_dates(work[date_col])
    for c in [open_col, high_col, low_col, close_col]:
        work[c] = pd.to_numeric(work[c], errors="coerce")
    if vol_col:
        work[vol_col] = pd.to_numeric(work[vol_col], errors="coerce")
    else:
        work["_VOL"] = 0
        vol_col = "_VOL"

    work = work.dropna(subset=[date_col, open_col, high_col, low_col, close_col])
    work = work.sort_values(date_col).drop_duplicates(date_col, keep="last")
    work = work.rename(columns={
        date_col: "date", open_col: "open", high_col: "high",
        low_col: "low", close_col: "close", vol_col: "volume",
    })
    work = work[["date", "open", "high", "low", "close", "volume"]].reset_index(drop=True)
    work, adjustments = adjust_corporate_actions(work)
    return work, adjustments


def completed_daily(hist):
    if hist is None or hist.empty:
        return hist
    out = hist.copy()
    now = pd.Timestamp.now(tz="Asia/Kolkata").tz_localize(None)
    # Never use a current-day historical row before the cash session has closed.
    if now.time() < pd.Timestamp("15:30").time():
        out = out[out["date"].dt.date < now.date()]
    else:
        out = out[out["date"].dt.date <= now.date()]
    return out.reset_index(drop=True)


def resample_ohlc(df, timeframe):
    if timeframe == "1D":
        return df.copy()
    rule = "W-FRI" if timeframe == "1W" else "ME"
    return (df.set_index("date").sort_index().resample(rule).agg({
        "open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"
    }).dropna(subset=["open", "high", "low", "close"]).reset_index())


def closed_higher_tf(hist, timeframe):
    daily = completed_daily(hist)
    if timeframe == "1D":
        return daily
    out = resample_ohlc(daily, timeframe)
    if out.empty:
        return out
    now = pd.Timestamp.now(tz="Asia/Kolkata").tz_localize(None)
    if timeframe == "1W":
        # Only a Friday-ended week that has actually closed.
        last_closed_friday = now.normalize() - pd.Timedelta(days=(now.weekday() - 4) % 7)
        if now.weekday() == 4 and now.time() < pd.Timestamp("15:30").time():
            last_closed_friday -= pd.Timedelta(days=7)
        out = out[out["date"] <= last_closed_friday]
    elif timeframe == "1M":
        month_end = now + pd.offsets.MonthEnd(0)
        if now.date() < month_end.date() or now.time() < pd.Timestamp("15:30").time():
            out = out[out["date"] < month_end.normalize()]
        else:
            out = out[out["date"] <= month_end.normalize()]
    return out.reset_index(drop=True)


def candle_parts(row):
    o, h, l, c = [float(row[k]) for k in ["open", "high", "low", "close"]]
    body = abs(c - o)
    rng = max(h - l, 1e-9)
    upper = h - max(o, c)
    lower = min(o, c) - l
    return o, h, l, c, body, rng, upper, lower


def prior_trend(df, lookback=5):
    if len(df) < lookback + 1:
        return "flat"
    closes = df.iloc[-(lookback + 1):-1]["close"].astype(float).tolist()
    slope = closes[-1] - closes[0]
    move = abs(slope) / max(abs(closes[0]), 1e-9)
    if move < 0.006:
        return "flat"
    return "up" if slope > 0 else "down"


def detect_patterns(df):
    if df is None or len(df) < 2:
        return []
    cur, prev = df.iloc[-1], df.iloc[-2]
    o, h, l, c, body, rng, upper, lower = candle_parts(cur)
    po, ph, pl, pc, pbody, prng, _, _ = candle_parts(prev)
    bullish, bearish = c > o, c < o
    p_bullish, p_bearish = pc > po, pc < po
    body_pct, prev_body_pct = body / rng, pbody / prng
    trend = prior_trend(df)
    found = []

    if len(df) >= 3:
        a, b, d = df.iloc[-3], df.iloc[-2], df.iloc[-1]
        ao, ah, al, ac, abody, arng, _, _ = candle_parts(a)
        bo, bh, bl, bc, bbody, brng, _, _ = candle_parts(b)
        do, dh, dl, dc, dbody, drng, _, _ = candle_parts(d)
        if ac < ao and abody/arng >= 0.55 and bbody/brng <= 0.30 and dc > do and dbody/drng >= 0.45 and dc > (ao+ac)/2 and trend == "down":
            found.append("Morning Star")
        if ac > ao and abody/arng >= 0.55 and bbody/brng <= 0.30 and dc < do and dbody/drng >= 0.45 and dc < (ao+ac)/2 and trend == "up":
            found.append("Evening Star")

    if p_bearish and bullish and body >= pbody * 1.05 and o <= pc and c >= po and prev_body_pct >= 0.30:
        found.append("Bullish Engulfing")
    if p_bullish and bearish and body >= pbody * 1.05 and o >= pc and c <= po and prev_body_pct >= 0.30:
        found.append("Bearish Engulfing")

    midpoint = (po + pc) / 2
    if p_bearish and bullish and o < pc and midpoint < c < po and body_pct >= 0.35 and prev_body_pct >= 0.45:
        found.append("Piercing Line")
    if p_bullish and bearish and o > pc and po < c < midpoint and body_pct >= 0.35 and prev_body_pct >= 0.45:
        found.append("Dark Cloud Cover")

    hammer_shape = lower >= max(body * 2.2, rng * 0.55) and upper <= rng * 0.12 and body_pct <= 0.40
    star_shape = upper >= max(body * 2.2, rng * 0.55) and lower <= rng * 0.12 and body_pct <= 0.40
    if hammer_shape and trend == "down": found.append("Hammer")
    if hammer_shape and trend == "up": found.append("Hanging Man")
    if star_shape and trend == "down": found.append("Inverted Hammer")
    if star_shape and trend == "up": found.append("Shooting Star")

    prev_top, prev_bottom = max(po, pc), min(po, pc)
    cur_top, cur_bottom = max(o, c), min(o, c)
    if p_bearish and bullish and body <= pbody * 0.60 and cur_top < prev_top and cur_bottom > prev_bottom:
        found.append("Bullish Harami")
    if p_bullish and bearish and body <= pbody * 0.60 and cur_top < prev_top and cur_bottom > prev_bottom:
        found.append("Bearish Harami")

    if body_pct >= 0.90 and upper/rng <= 0.05 and lower/rng <= 0.05:
        found.append("Bullish Marubozu" if bullish else "Bearish Marubozu")
    if body_pct <= 0.07:
        found.append("Doji")

    for name in PATTERN_PRIORITY:
        if name in found:
            return [name]
    return []


def pattern_snapshot(hist):
    out = {}
    for tf in TIMEFRAMES:
        if tf in INTRADAY_TFS:
            # NSE public chart_data is only an LTP line series, not true OHLC.
            # Do not invent candle wicks/patterns from it.
            out[tf] = {"patterns": [], "candle": None, "available": False, "reason": "true_intraday_ohlc_unavailable"}
            continue
        tf_df = closed_higher_tf(hist, tf)
        if tf_df is None or tf_df.empty:
            out[tf] = {"patterns": [], "candle": None, "available": False}
            continue
        row = tf_df.iloc[-1]
        out[tf] = {
            "patterns": detect_patterns(tf_df),
            "available": len(tf_df) >= 2,
            "candle": {
                "date": row["date"].strftime("%Y-%m-%d"),
                "open": clean_number(row["open"]), "high": clean_number(row["high"]),
                "low": clean_number(row["low"]), "close": clean_number(row["close"]),
            },
        }
    return out


def key_levels(hist, price):
    daily = completed_daily(hist)
    levels = {}
    if not daily.empty:
        # PDH/PDL = most recent COMPLETED trading day, not two sessions back.
        prev_day = daily.iloc[-1]
        levels["PDH"] = clean_number(prev_day["high"])
        levels["PDL"] = clean_number(prev_day["low"])

    now = pd.Timestamp.now(tz="Asia/Kolkata").tz_localize(None)
    py = daily[daily["date"].dt.year == now.year - 1]
    if not py.empty:
        levels["PYH"] = clean_number(py["high"].max())
        levels["PYL"] = clean_number(py["low"].min())

    q_start_month = 3 * (((now.month - 1) // 3)) + 1
    q_start = pd.Timestamp(now.year, q_start_month, 1)
    qrows = daily[daily["date"] >= q_start]
    if not qrows.empty:
        levels["QO"] = clean_number(qrows.iloc[0]["open"])

    details = {}
    for name, level in levels.items():
        d = distance_pct(price, level)
        details[name] = {"value": level, "distance_pct": d, "near": d is not None and d <= NEAR_LEVEL_PCT}
    return details


def scan_symbol(symbol, live):
    hist, adjustments = get_history(symbol)
    daily = completed_daily(hist)
    if daily.empty:
        raise ValueError("No completed NSE daily bars")

    close = daily["close"].astype(float).reset_index(drop=True)
    vol = daily["volume"].astype(float).reset_index(drop=True)
    latest = daily.iloc[-1]

    try:
        quote = live.stock_quote(symbol)
    except Exception:
        quote = {}
    price_info = quote.get("priceInfo", {}) if isinstance(quote, dict) else {}
    intra = price_info.get("intraDayHighLow", {}) or {}

    hist_price = clean_number(latest["close"])
    live_price = clean_number(price_info.get("lastPrice"))
    # Reject obviously broken/mismatched live quotes.
    if live_price is not None and hist_price and abs(live_price / hist_price - 1) <= 0.35:
        price = live_price
    else:
        price = hist_price

    prev = clean_number(price_info.get("previousClose"))
    if prev is None or (hist_price and abs(prev / hist_price - 1) > 0.35):
        prev = clean_number(close.iloc[-2]) if len(close) > 1 else hist_price

    sma20 = clean_number(close.rolling(20).mean().iloc[-1]) if len(close) >= 20 else None
    sma50 = clean_number(close.rolling(50).mean().iloc[-1]) if len(close) >= 50 else None
    sma200 = clean_number(close.rolling(200).mean().iloc[-1]) if len(close) >= 200 else None
    rsi14 = clean_number(rsi(close, 14).iloc[-1], 1) if len(close) >= 15 else None
    avgvol20 = clean_number(vol.rolling(20).mean().iloc[-1]) if len(vol) >= 20 else None
    lastvol = clean_number(vol.iloc[-1])
    volume_ratio = clean_number(lastvol / avgvol20) if lastvol is not None and avgvol20 and avgvol20 > 0 else None

    change = clean_number(price_info.get("pChange"))
    if change is None or abs(change) > 35:
        change = clean_number(((price - prev) / prev * 100) if prev else 0.0)

    d20, d50, d200 = distance_pct(price, sma20), distance_pct(price, sma50), distance_pct(price, sma200)
    tags, score = [], 0
    if sma20 is not None and price > sma20: tags.append("Above SMA20"); score += 1
    if sma20 is not None and sma50 is not None and sma20 > sma50: tags.append("SMA20 > SMA50"); score += 1
    if sma200 is not None and price > sma200: tags.append("Above SMA200")
    if rsi14 is not None and 50 <= rsi14 <= 70: tags.append("RSI Bullish"); score += 1
    if volume_ratio is not None and volume_ratio >= 1.5: tags.append("High Volume"); score += 1
    if d20 is not None and d20 <= NEAR_SMA_PCT: tags.append(f"Near SMA20 ({d20}%)")
    if d50 is not None and d50 <= NEAR_SMA_PCT: tags.append(f"Near SMA50 ({d50}%)")
    if d200 is not None and d200 <= NEAR_SMA_PCT: tags.append(f"Near SMA200 ({d200}%)")

    trend = "Bullish" if sma20 is not None and sma50 is not None and price > sma20 > sma50 else (
        "Bearish" if sma20 is not None and sma50 is not None and price < sma20 < sma50 else "Mixed"
    )

    levels = key_levels(hist, price)
    for name, info in levels.items():
        if info.get("near"):
            tags.append(f"Near {name} ({info['distance_pct']}%)")

    open_px = clean_number(price_info.get("open")) or clean_number(latest["open"])
    day_high = clean_number(intra.get("max")) or clean_number(latest["high"])
    day_low = clean_number(intra.get("min")) or clean_number(latest["low"])
    vwap = clean_number(price_info.get("vwap"))

    chart = [{"date": r["date"].strftime("%Y-%m-%d"), "close": clean_number(r["close"])} for _, r in daily.tail(CHART_POINTS).iterrows()]
    return {
        "symbol": symbol, "price": price, "change_pct": change,
        "open": open_px, "previous_close": prev, "day_high": day_high, "day_low": day_low, "vwap": vwap,
        "sma20": sma20, "sma50": sma50, "sma200": sma200,
        "sma20_distance_pct": d20, "sma50_distance_pct": d50, "sma200_distance_pct": d200,
        "near_sma20": d20 is not None and d20 <= NEAR_SMA_PCT,
        "near_sma50": d50 is not None and d50 <= NEAR_SMA_PCT,
        "near_sma200": d200 is not None and d200 <= NEAR_SMA_PCT,
        "rsi14": rsi14, "volume_ratio": volume_ratio, "trend": trend, "score": score,
        "signals": tags, "patterns": pattern_snapshot(hist), "levels": levels,
        "chart": chart, "source": "NSE India", "last_completed_session": latest["date"].strftime("%Y-%m-%d"),
        "corporate_action_adjustments": adjustments,
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
        "market": "NSE", "source": "NSE India", "count": len(rows),
        "near_sma_pct": NEAR_SMA_PCT, "near_level_pct": NEAR_LEVEL_PCT,
        "pattern_timeframes": TIMEFRAMES, "pattern_names": PATTERN_PRIORITY,
        "key_levels": ["PDH", "PDL", "QO", "PYH", "PYL"],
        "intraday_pattern_status": "disabled_until_true_nse_ohlc_source_is_available",
        "results": rows, "errors": errors,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, indent=2, allow_nan=False), encoding="utf-8")
    print(f"Wrote {len(rows)} NSE stocks to {OUT}; errors={len(errors)}")


if __name__ == "__main__":
    main()
