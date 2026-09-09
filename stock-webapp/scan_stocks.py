import json
import math
from pathlib import Path
from datetime import datetime, timezone
import yfinance as yf

BASE = Path(__file__).resolve().parent
SYMBOLS_FILE = BASE / "stock_symbols.txt"
OUT = BASE / "data" / "results.json"


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


def scan_symbol(symbol):
    df = yf.download(symbol, period="6mo", interval="1d", auto_adjust=False, progress=False, threads=False)
    if df is None or df.empty:
        raise ValueError("No data")
    if hasattr(df.columns, "levels"):
        df.columns = df.columns.get_level_values(0)
    close = df["Close"].astype(float)
    vol = df["Volume"].astype(float)
    price = clean_number(close.iloc[-1])
    prev = clean_number(close.iloc[-2]) if len(close) > 1 else price
    sma20 = clean_number(close.rolling(20).mean().iloc[-1]) if len(close) >= 20 else None
    sma50 = clean_number(close.rolling(50).mean().iloc[-1]) if len(close) >= 50 else None
    rsi14 = clean_number(rsi(close, 14).iloc[-1], 1) if len(close) >= 15 else None
    avgvol20 = clean_number(vol.rolling(20).mean().iloc[-1]) if len(vol) >= 20 else None
    lastvol = clean_number(vol.iloc[-1])
    volume_ratio = clean_number(lastvol / avgvol20) if lastvol is not None and avgvol20 is not None and avgvol20 > 0 else None

    # If Yahoo returns an incomplete latest row, use the most recent finite close.
    if price is None:
        finite_close = close[close.notna()]
        if finite_close.empty:
            raise ValueError("No valid close price")
        price = clean_number(finite_close.iloc[-1])
        prev = clean_number(finite_close.iloc[-2]) if len(finite_close) > 1 else price

    change = ((price - prev) / prev * 100) if prev else 0.0

    tags = []
    score = 0
    if sma20 is not None and price > sma20:
        tags.append("Above SMA20"); score += 1
    if sma20 is not None and sma50 is not None and sma20 > sma50:
        tags.append("SMA20 > SMA50"); score += 1
    if rsi14 is not None and 50 <= rsi14 <= 70:
        tags.append("RSI Bullish"); score += 1
    if volume_ratio is not None and volume_ratio >= 1.5:
        tags.append("High Volume"); score += 1

    trend = "Bullish" if sma20 is not None and sma50 is not None and price > sma20 > sma50 else (
        "Bearish" if sma20 is not None and sma50 is not None and price < sma20 < sma50 else "Mixed"
    )

    return {
        "symbol": symbol.replace(".NS", ""),
        "yahoo_symbol": symbol,
        "price": price,
        "change_pct": clean_number(change),
        "sma20": sma20,
        "sma50": sma50,
        "rsi14": rsi14,
        "volume_ratio": volume_ratio,
        "trend": trend,
        "score": score,
        "signals": tags,
    }


def main():
    symbols = [x.strip() for x in SYMBOLS_FILE.read_text(encoding="utf-8").splitlines() if x.strip() and not x.startswith("#")]
    rows, errors = [], []
    for symbol in symbols:
        try:
            rows.append(scan_symbol(symbol))
        except Exception as e:
            errors.append({"symbol": symbol, "error": str(e)})
    rows.sort(key=lambda x: (x["score"], x["change_pct"] if x["change_pct"] is not None else -999999), reverse=True)
    payload = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "market": "NSE",
        "count": len(rows),
        "results": rows,
        "errors": errors,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, indent=2, allow_nan=False), encoding="utf-8")
    print(f"Wrote {len(rows)} stocks to {OUT}; errors={len(errors)}")


if __name__ == "__main__":
    main()
