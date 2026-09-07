import os
from datetime import datetime, timezone

import requests
import yfinance as yf

DISTANCE_PERCENT = 0.10

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
    """Return latest confirmed meaningful pivot high and low.

    A swing must be the highest/lowest point in a 7-bar window. This deliberately
    ignores tiny 1-bar internal pullbacks and uses only confirmed pivots.
    """
    if df is None or df.empty or len(df) < left + right + 1:
        return None, None

    data = df.dropna(subset=["High", "Low"])
    highs = data["High"].astype(float)
    lows = data["Low"].astype(float)
    swing_high = None
    swing_low = None

    for i in range(left, len(data) - right):
        h = float(highs.iloc[i])
        l = float(lows.iloc[i])
        if h >= float(highs.iloc[i-left:i+right+1].max()):
            swing_high = h
        if l <= float(lows.iloc[i-left:i+right+1].min()):
            swing_low = l

    return swing_high, swing_low


def get_history(ticker, period, interval):
    df = ticker.history(period=period, interval=interval, auto_adjust=False)
    if df is None or df.empty:
        return df
    return df.dropna(subset=["Open", "High", "Low", "Close"])


def get_levels(symbol: str):
    ticker = yf.Ticker(symbol)
    daily = get_history(ticker, "3y", "1d")
    intra = ticker.history(period="5d", interval="5m", auto_adjust=False).dropna(subset=["Close"])

    if daily is None or daily.empty or intra.empty or len(intra) < 2:
        raise ValueError("Not enough market data")

    current_close = float(intra["Close"].iloc[-1])
    previous_close = float(intra["Close"].iloc[-2])
    current_date = intra.index[-1].date()
    current_year = current_date.year
    current_quarter = (current_date.month - 1) // 3 + 1

    # Existing key levels.
    if daily.index[-1].date() >= current_date and len(daily) >= 2:
        prev_day_row = daily.iloc[-2]
    else:
        prev_day_row = daily.iloc[-1]

    year_rows = daily[[idx.year == current_year for idx in daily.index]]
    quarter_rows = daily[[idx.year == current_year and ((idx.month - 1)//3 + 1) == current_quarter for idx in daily.index]]
    prev_year_rows = daily[[idx.year == current_year - 1 for idx in daily.index]]

    levels = {
        "PDH": float(prev_day_row["High"]),
        "PDL": float(prev_day_row["Low"]),
        "QO": float(quarter_rows["Open"].iloc[0]) if not quarter_rows.empty else None,
        "YEAR OPEN": float(year_rows["Open"].iloc[0]) if not year_rows.empty else None,
        "PYH": float(prev_year_rows["High"].max()) if not prev_year_rows.empty else None,
        "PYL": float(prev_year_rows["Low"].min()) if not prev_year_rows.empty else None,
    }

    # External/extreme swing levels from every major timeframe >= 1H.
    # Yahoo supports 1H directly. Higher TFs are resampled from 1H/daily data.
    hourly = get_history(ticker, "730d", "1h")
    if hourly is not None and not hourly.empty:
        timeframe_frames = {
            "1H": hourly,
            "4H": hourly.resample("4h").agg({"Open":"first", "High":"max", "Low":"min", "Close":"last"}).dropna(),
        }
    else:
        timeframe_frames = {}

    timeframe_frames["1D"] = daily
    timeframe_frames["1W"] = daily.resample("W").agg({"Open":"first", "High":"max", "Low":"min", "Close":"last"}).dropna()
    timeframe_frames["1M"] = daily.resample("ME").agg({"Open":"first", "High":"max", "Low":"min", "Close":"last"}).dropna()

    for tf, frame in timeframe_frames.items():
        sh, sl = last_external_swings(frame)
        levels[f"{tf} EXT SWING HIGH"] = sh
        levels[f"{tf} EXT SWING LOW"] = sl

    return current_close, previous_close, levels, intra.index[-1]


def clean_symbol(symbol):
    names = {"GC=F": "GOLD", "BTC-USD": "BTCUSD"}
    return names.get(symbol, symbol.replace("=X", ""))


def scan_symbol(symbol: str):
    current, previous, levels, timestamp = get_levels(symbol)
    alerts = []

    for name, level in levels.items():
        if level is None:
            continue
        if near(current, level) and not near(previous, level):
            alerts.append((name, level, distance_pct(current, level)))

    for name, level, dist in alerts:
        message = (
            f"🔔 {clean_symbol(symbol)} near {name}\n"
            f"Price: {current:.5f}\n"
            f"Level: {level:.5f}\n"
            f"Distance: {dist:.3f}%\n"
            f"Time: {timestamp}\n"
            "Important level — chart check karo."
        )
        send_telegram(message)

    return len(alerts)


def main():
    symbols = read_symbols()
    print(f"Scanning {len(symbols)} symbols at {datetime.now(timezone.utc).isoformat()}")
    total_alerts, successful = 0, 0
    errors = []

    for symbol in symbols:
        try:
            count = scan_symbol(symbol)
            total_alerts += count
            successful += 1
            print(f"{symbol}: OK, alerts={count}")
        except Exception as e:
            errors.append(f"{symbol}: {e}")
            print(f"{symbol}: ERROR: {e}")

    print(f"Done. Total alerts sent: {total_alerts}")
    if errors:
        print("Errors:")
        for err in errors:
            print(" -", err)

    if RUN_REASON == "workflow_dispatch":
        send_telegram(
            "✅ Rupak Forex Scanner working\n"
            f"Symbols checked: {successful}/{len(symbols)}\n"
            "Levels: PDH/PDL, QO, Year Open, PYH/PYL + 1H/4H/1D/1W/1M external swings\n"
            f"Alerts this run: {total_alerts}\nErrors: {len(errors)}"
        )


if __name__ == "__main__":
    main()
