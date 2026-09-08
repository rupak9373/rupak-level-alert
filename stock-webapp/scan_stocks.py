import json
from pathlib import Path
from datetime import datetime, timezone
import yfinance as yf

BASE = Path(__file__).resolve().parent
SYMBOLS_FILE = BASE / "stock_symbols.txt"
OUT = BASE / "data" / "results.json"


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
    high = df["High"].astype(float)
    low = df["Low"].astype(float)
    vol = df["Volume"].astype(float)
    price = float(close.iloc[-1])
    prev = float(close.iloc[-2]) if len(close) > 1 else price
    sma20 = float(close.rolling(20).mean().iloc[-1]) if len(close) >= 20 else None
    sma50 = float(close.rolling(50).mean().iloc[-1]) if len(close) >= 50 else None
    rsi14 = float(rsi(close, 14).iloc[-1]) if len(close) >= 15 else None
    avgvol20 = float(vol.rolling(20).mean().iloc[-1]) if len(vol) >= 20 else None
    volume_ratio = float(vol.iloc[-1] / avgvol20) if avgvol20 and avgvol20 > 0 else None
    high20 = float(high.tail(20).max()) if len(high) >= 20 else float(high.max())
    low20 = float(low.tail(20).min()) if len(low) >= 20 else float(low.min())
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
    if high20 and price >= high20 * 0.995:
        tags.append("20D Breakout Zone"); score += 2
    if low20 and price <= low20 * 1.005:
        tags.append("20D Support Zone")

    trend = "Bullish" if sma20 is not None and sma50 is not None and price > sma20 > sma50 else (
        "Bearish" if sma20 is not None and sma50 is not None and price < sma20 < sma50 else "Mixed"
    )

    return {
        "symbol": symbol.replace(".NS", ""),
        "yahoo_symbol": symbol,
        "price": round(price, 2),
        "change_pct": round(change, 2),
        "sma20": round(sma20, 2) if sma20 is not None else None,
        "sma50": round(sma50, 2) if sma50 is not None else None,
        "rsi14": round(rsi14, 1) if rsi14 is not None else None,
        "volume_ratio": round(volume_ratio, 2) if volume_ratio is not None else None,
        "high20": round(high20, 2),
        "low20": round(low20, 2),
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
    rows.sort(key=lambda x: (x["score"], x["change_pct"]), reverse=True)
    payload = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "market": "NSE",
        "count": len(rows),
        "results": rows,
        "errors": errors,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"Wrote {len(rows)} stocks to {OUT}; errors={len(errors)}")


if __name__ == "__main__":
    main()
