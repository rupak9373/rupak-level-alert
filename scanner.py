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
    if level is None or level == 0:
        return False
    return abs(price - level) / abs(level) * 100 <= DISTANCE_PERCENT


def distance_pct(price, level):
    return abs(price - level) / abs(level) * 100


def get_levels(symbol: str):
    ticker = yf.Ticker(symbol)

    daily = ticker.history(period="3y", interval="1d", auto_adjust=False)
    intra = ticker.history(period="5d", interval="5m", auto_adjust=False)

    if daily.empty or intra.empty or len(intra) < 2:
        raise ValueError("Not enough market data")

    daily = daily.dropna(subset=["Open", "High", "Low", "Close"])
    intra = intra.dropna(subset=["Close"])

    if daily.empty or len(intra) < 2:
        raise ValueError("Not enough clean market data")

    current_close = float(intra["Close"].iloc[-1])
    previous_close = float(intra["Close"].iloc[-2])

    current_date = intra.index[-1].date()
    current_year = current_date.year
    current_quarter = (current_date.month - 1) // 3 + 1

    # Previous completed trading day.
    daily_dates = [idx.date() for idx in daily.index]
    if daily_dates[-1] >= current_date and len(daily) >= 2:
        prev_day_row = daily.iloc[-2]
    else:
        prev_day_row = daily.iloc[-1]

    pdh = float(prev_day_row["High"])
    pdl = float(prev_day_row["Low"])

    # Current year open.
    year_mask = [idx.year == current_year for idx in daily.index]
    current_year_rows = daily[year_mask]
    yo = float(current_year_rows["Open"].iloc[0]) if not current_year_rows.empty else None

    # Current quarter open.
    quarter_mask = [
        idx.year == current_year and ((idx.month - 1) // 3 + 1) == current_quarter
        for idx in daily.index
    ]
    current_quarter_rows = daily[quarter_mask]
    qo = float(current_quarter_rows["Open"].iloc[0]) if not current_quarter_rows.empty else None

    # Previous calendar year's high/low.
    previous_year_mask = [idx.year == current_year - 1 for idx in daily.index]
    previous_year_rows = daily[previous_year_mask]
    pyh = float(previous_year_rows["High"].max()) if not previous_year_rows.empty else None
    pyl = float(previous_year_rows["Low"].min()) if not previous_year_rows.empty else None

    levels = {
        "PDH": pdh,
        "PDL": pdl,
        "QO": qo,
        "YEAR OPEN": yo,
        "PYH": pyh,
        "PYL": pyl,
    }

    return current_close, previous_close, levels, intra.index[-1]


def scan_symbol(symbol: str):
    current, previous, levels, timestamp = get_levels(symbol)
    alerts = []

    for name, level in levels.items():
        if level is None:
            continue

        # Send an alert only when price ENTERS the configured zone.
        # This avoids a Telegram message every 5 minutes while price stays nearby.
        now_near = near(current, level)
        was_near = near(previous, level)

        if now_near and not was_near:
            alerts.append((name, level, distance_pct(current, level)))

    for name, level, dist in alerts:
        message = (
            f"🔔 {symbol} near {name}\n"
            f"Price: {current:.4f}\n"
            f"Level: {level:.4f}\n"
            f"Distance: {dist:.3f}%\n"
            f"Time: {timestamp}\n"
            "Chart check karo."
        )
        send_telegram(message)

    return len(alerts)


def main():
    symbols = read_symbols()
    started = datetime.now(timezone.utc)
    print(f"Scanning {len(symbols)} symbols at {started.isoformat()}")

    total_alerts = 0
    errors = []
    successful = 0

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

    # Manual GitHub run = connection test. Scheduled runs stay silent unless a level alert occurs.
    if RUN_REASON == "workflow_dispatch":
        test_text = (
            "✅ Rupak Level Scanner working\n"
            f"Symbols checked successfully: {successful}/{len(symbols)}\n"
            f"Level alerts this run: {total_alerts}\n"
            f"Errors: {len(errors)}\n"
            "Automatic scan active hai."
        )
        send_telegram(test_text)
        print("Manual-run Telegram test message sent.")


if __name__ == "__main__":
    main()
