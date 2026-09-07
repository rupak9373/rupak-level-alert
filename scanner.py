import os
from datetime import datetime, timezone

import requests
import yfinance as yf

DISTANCE_PERCENT = 0.10
SMA_LENGTH = 20

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
RUN_REASON = os.environ.get("RUN_REASON", "schedule")


def send_telegram(text: str):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    response = requests.post(url, json={"chat_id": CHAT_ID, "text": text}, timeout=20)
    response.raise_for_status()


def read_symbols():
    with open("symbols.txt", "r", encoding="utf-8") as f:
        return [line.strip() for line in f if line.strip() and not line.strip().startswith("#")]


def near(price, level):
    return level not in (None, 0) and abs(price - level) / abs(level) * 100 <= DISTANCE_PERCENT


def distance_pct(price, level):
    return abs(price - level) / abs(level) * 100


def last_external_swings(df, left=3, right=3):
    if df is None or df.empty or len(df) < left + right + 1:
        return None, None
    data = df.dropna(subset=["High", "Low"])
    highs, lows = data["High"].astype(float), data["Low"].astype(float)
    swing_high = swing_low = None
    for i in range(left, len(data) - right):
        h, l = float(highs.iloc[i]), float(lows.iloc[i])
        if h >= float(highs.iloc[i-left:i+right+1].max()): swing_high = h
        if l <= float(lows.iloc[i-left:i+right+1].min()): swing_low = l
    return swing_high, swing_low


def get_history(ticker, period, interval):
    df = ticker.history(period=period, interval=interval, auto_adjust=False)
    if df is None or df.empty: return df
    return df.dropna(subset=["Open", "High", "Low", "Close"])


def get_levels(symbol: str):
    ticker = yf.Ticker(symbol)
    daily = get_history(ticker, "3y", "1d")
    intra = ticker.history(period="5d", interval="5m", auto_adjust=False).dropna(subset=["Close"])
    m15 = ticker.history(period="30d", interval="15m", auto_adjust=False).dropna(subset=["Close"])

    if daily is None or daily.empty or intra.empty or len(intra) < 2:
        raise ValueError("Not enough market data")

    current_close = float(intra["Close"].iloc[-1])
    previous_close = float(intra["Close"].iloc[-2])
    current_date = intra.index[-1].date()
    current_year = current_date.year
    current_quarter = (current_date.month - 1) // 3 + 1

    # 15m SMA20 is context only; it never blocks a key-level alert.
    sma20 = None
    trend_context = "SMA data unavailable"
    if not m15.empty and len(m15) >= SMA_LENGTH:
        sma20 = float(m15["Close"].rolling(SMA_LENGTH).mean().iloc[-1])
        if current_close > sma20:
            trend_context = "Price above 15m SMA20 → Bullish context"
        elif current_close < sma20:
            trend_context = "Price below 15m SMA20 → Bearish context"
        else:
            trend_context = "Price at 15m SMA20 → Neutral context"

    if daily.index[-1].date() >= current_date and len(daily) >= 2:
        prev_day_row = daily.iloc[-2]
    else:
        prev_day_row = daily.iloc[-1]

    year_rows = daily[[idx.year == current_year for idx in daily.index]]
    quarter_rows = daily[[idx.year == current_year and ((idx.month - 1)//3 + 1) == current_quarter for idx in daily.index]]
    prev_year_rows = daily[[idx.year == current_year - 1 for idx in daily.index]]

    levels = {
        "PDH": float(prev_day_row["High"]), "PDL": float(prev_day_row["Low"]),
        "QO": float(quarter_rows["Open"].iloc[0]) if not quarter_rows.empty else None,
        "YEAR OPEN": float(year_rows["Open"].iloc[0]) if not year_rows.empty else None,
        "PYH": float(prev_year_rows["High"].max()) if not prev_year_rows.empty else None,
        "PYL": float(prev_year_rows["Low"].min()) if not prev_year_rows.empty else None,
    }

    hourly = get_history(ticker, "730d", "1h")
    timeframe_frames = {}
    if hourly is not None and not hourly.empty:
        timeframe_frames["1H"] = hourly
        timeframe_frames["4H"] = hourly.resample("4h").agg({"Open":"first","High":"max","Low":"min","Close":"last"}).dropna()
    timeframe_frames["1D"] = daily
    timeframe_frames["1W"] = daily.resample("W").agg({"Open":"first","High":"max","Low":"min","Close":"last"}).dropna()
    timeframe_frames["1M"] = daily.resample("ME").agg({"Open":"first","High":"max","Low":"min","Close":"last"}).dropna()

    for tf, frame in timeframe_frames.items():
        sh, sl = last_external_swings(frame)
        levels[f"{tf} EXT SWING HIGH"] = sh
        levels[f"{tf} EXT SWING LOW"] = sl

    return current_close, previous_close, levels, intra.index[-1], sma20, trend_context


def clean_symbol(symbol):
    return {"GC=F":"GOLD", "BTC-USD":"BTCUSD"}.get(symbol, symbol.replace("=X", ""))


def scan_symbol(symbol: str):
    current, previous, levels, timestamp, sma20, trend_context = get_levels(symbol)
    alerts = []
    for name, level in levels.items():
        if level is not None and near(current, level) and not near(previous, level):
            alerts.append((name, level, distance_pct(current, level)))

    for name, level, dist in alerts:
        sma_line = f"15m SMA20: {sma20:.5f}\n{trend_context}\n" if sma20 is not None else f"{trend_context}\n"
        send_telegram(
            f"🔔 {clean_symbol(symbol)} near {name}\n"
            f"Price: {current:.5f}\nLevel: {level:.5f}\nDistance: {dist:.3f}%\n"
            f"{sma_line}Time: {timestamp}\nImportant level — chart check karo."
        )
    return len(alerts)


def main():
    symbols = read_symbols()
    print(f"Scanning {len(symbols)} symbols at {datetime.now(timezone.utc).isoformat()}")
    total_alerts, successful, errors = 0, 0, []
    for symbol in symbols:
        try:
            count = scan_symbol(symbol); total_alerts += count; successful += 1
            print(f"{symbol}: OK, alerts={count}")
        except Exception as e:
            errors.append(f"{symbol}: {e}"); print(f"{symbol}: ERROR: {e}")
    print(f"Done. Total alerts sent: {total_alerts}")
    if RUN_REASON == "workflow_dispatch":
        send_telegram(
            "✅ Rupak Forex Scanner working\n"
            f"Symbols checked: {successful}/{len(symbols)}\n"
            "Levels + external swings active\n15m SMA20 trend context active\n"
            f"Alerts this run: {total_alerts}\nErrors: {len(errors)}"
        )


if __name__ == "__main__":
    main()
