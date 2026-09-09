import gzip
import json
import math
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

import pandas as pd
import requests

BASE = Path(__file__).resolve().parent
RESULTS = BASE / "data" / "results.json"
TOKEN = os.getenv("UPSTOX_ACCESS_TOKEN", "").strip()
INSTRUMENTS_URL = "https://assets.upstox.com/market-quote/instruments/exchange/NSE.json.gz"
TF_MAP = {
    "15m": ("minutes", 15, timedelta(minutes=15)),
    "30m": ("minutes", 30, timedelta(minutes=30)),
    "1H": ("hours", 1, timedelta(hours=1)),
    "2H": ("hours", 2, timedelta(hours=2)),
    "4H": ("hours", 4, timedelta(hours=4)),
}
PATTERN_PRIORITY = [
    "Morning Star", "Evening Star", "Bullish Engulfing", "Bearish Engulfing",
    "Piercing Line", "Dark Cloud Cover", "Hammer", "Hanging Man",
    "Inverted Hammer", "Shooting Star", "Bullish Harami", "Bearish Harami",
    "Bullish Marubozu", "Bearish Marubozu", "Doji",
]


def num(v):
    try:
        x = float(v)
        return x if math.isfinite(x) else None
    except Exception:
        return None


def load_instrument_keys():
    r = requests.get(INSTRUMENTS_URL, timeout=30)
    r.raise_for_status()
    raw = gzip.decompress(r.content)
    items = json.loads(raw.decode("utf-8"))
    out = {}
    for x in items:
        if x.get("segment") == "NSE_EQ" and x.get("instrument_type") == "EQ":
            sym = str(x.get("trading_symbol") or "").upper().strip()
            key = x.get("instrument_key")
            if sym and key:
                out[sym] = key
    return out


def parse_candles(candles):
    rows = []
    for c in candles or []:
        if not isinstance(c, list) or len(c) < 5:
            continue
        try:
            ts = pd.to_datetime(c[0], utc=True).tz_convert("Asia/Kolkata")
        except Exception:
            continue
        o, h, l, cl = map(num, c[1:5])
        if None in (o, h, l, cl):
            continue
        rows.append({"date": ts, "open": o, "high": h, "low": l, "close": cl})
    if not rows:
        return pd.DataFrame(columns=["date", "open", "high", "low", "close"])
    return pd.DataFrame(rows).sort_values("date").drop_duplicates("date", keep="last").reset_index(drop=True)


def get_json(url):
    headers = {"Accept": "application/json", "Authorization": f"Bearer {TOKEN}"}
    r = requests.get(url, headers=headers, timeout=25)
    if r.status_code != 200:
        raise RuntimeError(f"Upstox HTTP {r.status_code}: {r.text[:160]}")
    return r.json()


def fetch_tf(key, unit, interval, candle_span):
    now = pd.Timestamp.now(tz="Asia/Kolkata")
    encoded = quote(key, safe="")
    # Historical chunk through yesterday provides a stable base.
    to_date = (now.date() - timedelta(days=1)).isoformat()
    from_date = (now.date() - timedelta(days=45)).isoformat()
    hist_url = f"https://api.upstox.com/v3/historical-candle/{encoded}/{unit}/{interval}/{to_date}/{from_date}"
    hist = get_json(hist_url).get("data", {}).get("candles", [])

    # Current-session endpoint is merged in, then only fully closed candles are kept.
    intra_url = f"https://api.upstox.com/v3/historical-candle/intraday/{encoded}/{unit}/{interval}"
    try:
        intra = get_json(intra_url).get("data", {}).get("candles", [])
    except Exception:
        intra = []

    df = parse_candles((hist or []) + (intra or []))
    if df.empty:
        return df
    cutoff = now
    df = df[df["date"].apply(lambda t: t + candle_span <= cutoff)].reset_index(drop=True)
    return df


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
        if ac < ao and abody/arng >= 0.55 and bbody/brng <= 0.30 and dc > do and dbody/drng >= 0.45 and dc > (ao+ac)/2 and trend == "down": found.append("Morning Star")
        if ac > ao and abody/arng >= 0.55 and bbody/brng <= 0.30 and dc < do and dbody/drng >= 0.45 and dc < (ao+ac)/2 and trend == "up": found.append("Evening Star")
    if p_bearish and bullish and body >= pbody * 1.05 and o <= pc and c >= po and prev_body_pct >= 0.30: found.append("Bullish Engulfing")
    if p_bullish and bearish and body >= pbody * 1.05 and o >= pc and c <= po and prev_body_pct >= 0.30: found.append("Bearish Engulfing")
    midpoint = (po + pc) / 2
    if p_bearish and bullish and o < pc and midpoint < c < po and body_pct >= 0.35 and prev_body_pct >= 0.45: found.append("Piercing Line")
    if p_bullish and bearish and o > pc and po < c < midpoint and body_pct >= 0.35 and prev_body_pct >= 0.45: found.append("Dark Cloud Cover")
    hammer_shape = lower >= max(body * 2.2, rng * 0.55) and upper <= rng * 0.12 and body_pct <= 0.40
    star_shape = upper >= max(body * 2.2, rng * 0.55) and lower <= rng * 0.12 and body_pct <= 0.40
    if hammer_shape and trend == "down": found.append("Hammer")
    if hammer_shape and trend == "up": found.append("Hanging Man")
    if star_shape and trend == "down": found.append("Inverted Hammer")
    if star_shape and trend == "up": found.append("Shooting Star")
    prev_top, prev_bottom = max(po, pc), min(po, pc)
    cur_top, cur_bottom = max(o, c), min(o, c)
    if p_bearish and bullish and body <= pbody * 0.60 and cur_top < prev_top and cur_bottom > prev_bottom: found.append("Bullish Harami")
    if p_bullish and bearish and body <= pbody * 0.60 and cur_top < prev_top and cur_bottom > prev_bottom: found.append("Bearish Harami")
    if body_pct >= 0.90 and upper/rng <= 0.05 and lower/rng <= 0.05: found.append("Bullish Marubozu" if bullish else "Bearish Marubozu")
    if body_pct <= 0.07: found.append("Doji")
    for name in PATTERN_PRIORITY:
        if name in found:
            return [name]
    return []


def main():
    payload = json.loads(RESULTS.read_text(encoding="utf-8"))
    results = payload.get("results", [])
    errors = payload.setdefault("intraday_errors", [])
    payload["intraday_source"] = "Upstox V3 historical/intraday candles"

    if not TOKEN:
        payload["intraday_status"] = "UPSTOX_ACCESS_TOKEN missing"
        for x in results:
            for tf in TF_MAP:
                x.setdefault("patterns", {})[tf] = {"patterns": [], "candle": None, "available": False, "reason": "upstox_access_token_missing"}
        RESULTS.write_text(json.dumps(payload, indent=2, allow_nan=False), encoding="utf-8")
        print("Upstox token missing; intraday data left unavailable")
        return

    try:
        keys = load_instrument_keys()
    except Exception as e:
        payload["intraday_status"] = f"instrument master failed: {e}"
        RESULTS.write_text(json.dumps(payload, indent=2, allow_nan=False), encoding="utf-8")
        raise

    ok = 0
    for x in results:
        symbol = str(x.get("symbol") or "").upper()
        key = keys.get(symbol)
        if not key:
            errors.append({"symbol": symbol, "stage": "instrument_key", "error": "not found"})
            continue
        for tf, (unit, interval, span) in TF_MAP.items():
            try:
                df = fetch_tf(key, unit, interval, span)
                if df.empty:
                    raise RuntimeError("no closed candles")
                row = df.iloc[-1]
                x.setdefault("patterns", {})[tf] = {
                    "patterns": detect_patterns(df),
                    "available": len(df) >= 2,
                    "source": "Upstox V3",
                    "closed_candle_only": True,
                    "candle": {
                        "date": row["date"].isoformat(),
                        "open": round(float(row["open"]), 2),
                        "high": round(float(row["high"]), 2),
                        "low": round(float(row["low"]), 2),
                        "close": round(float(row["close"]), 2),
                    },
                }
                ok += 1
            except Exception as e:
                errors.append({"symbol": symbol, "timeframe": tf, "stage": "upstox", "error": str(e)})
                x.setdefault("patterns", {})[tf] = {"patterns": [], "candle": None, "available": False, "reason": "upstox_fetch_failed"}

    payload["intraday_status"] = f"enriched {ok} symbol-timeframes"
    payload["intraday_updated_at"] = datetime.now(timezone.utc).isoformat()
    RESULTS.write_text(json.dumps(payload, indent=2, allow_nan=False), encoding="utf-8")
    print(payload["intraday_status"])


if __name__ == "__main__":
    main()
