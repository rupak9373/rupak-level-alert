import os
from datetime import datetime, timezone

import requests
import yfinance as yf

DISTANCE_PERCENT = 0.10
SMA_LENGTHS = [20, 50, 200]
EXTERNAL_SWING_LOOKBACK = 10

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
RUN_REASON = os.environ.get("RUN_REASON", "schedule")


def send_telegram(text: str):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    r = requests.post(url, json={"chat_id": CHAT_ID, "text": text}, timeout=20)
    r.raise_for_status()


def read_symbols():
    with open("symbols.txt", "r", encoding="utf-8") as f:
        return [x.strip() for x in f if x.strip() and not x.startswith("#")]


def near(price, level):
    return level not in (None, 0) and abs(price-level)/abs(level)*100 <= DISTANCE_PERCENT


def crossed_or_entered(prev, cur, level):
    if level in (None, 0):
        return False
    return (near(cur, level) and not near(prev, level)) or ((prev-level)*(cur-level) <= 0 and prev != cur)


def h1_external_swings(df, lookback=EXTERNAL_SWING_LOOKBACK):
    if df is None or df.empty or len(df) < lookback*2+1:
        return None, None
    d = df.dropna(subset=["High", "Low"])
    sh = sl = None
    for i in range(lookback, len(d)-lookback):
        window = d.iloc[i-lookback:i+lookback+1]
        h = float(d["High"].iloc[i]); l = float(d["Low"].iloc[i])
        if h >= float(window["High"].max()): sh = h
        if l <= float(window["Low"].min()): sl = l
    return sh, sl


def history(t, period, interval):
    d = t.history(period=period, interval=interval, auto_adjust=False)
    if d is None or d.empty: return d
    return d.dropna(subset=["Open", "High", "Low", "Close"])


def clean_symbol(s):
    return {"GC=F":"XAUUSD/GOLD", "CL=F":"USOIL", "BTC-USD":"BTCUSD"}.get(s, s.replace("=X", ""))


def get_data(symbol):
    t = yf.Ticker(symbol)
    daily = history(t, "3y", "1d")
    m5 = history(t, "5d", "5m")
    m15 = history(t, "60d", "15m")
    h1 = history(t, "730d", "1h")
    if daily is None or daily.empty or m5 is None or len(m5) < 2:
        raise ValueError("Not enough market data")

    cur = float(m5["Close"].iloc[-1]); prev = float(m5["Close"].iloc[-2])
    date = m5.index[-1].date(); year = date.year; quarter = (date.month-1)//3+1

    prev_day = daily.iloc[-2] if daily.index[-1].date() >= date and len(daily)>=2 else daily.iloc[-1]
    qrows = daily[[i.year==year and ((i.month-1)//3+1)==quarter for i in daily.index]]
    pyrows = daily[[i.year==year-1 for i in daily.index]]

    levels = {
        "PDH": float(prev_day["High"]),
        "PDL": float(prev_day["Low"]),
        "QO": float(qrows["Open"].iloc[0]) if not qrows.empty else None,
        "PYH": float(pyrows["High"].max()) if not pyrows.empty else None,
        "PYL": float(pyrows["Low"].min()) if not pyrows.empty else None,
    }

    sh, sl = h1_external_swings(h1)
    levels["H1 EXT SWING HIGH"] = sh
    levels["H1 EXT SWING LOW"] = sl

    smas = {}
    if m15 is not None and not m15.empty:
        for n in SMA_LENGTHS:
            if len(m15) >= n:
                smas[f"15M SMA {n}"] = float(m15["Close"].rolling(n).mean().iloc[-1])

    return cur, prev, levels, smas, m5.index[-1]


def scan_symbol(symbol):
    cur, prev, levels, smas, ts = get_data(symbol)
    hits = []
    for name, level in {**levels, **smas}.items():
        if crossed_or_entered(prev, cur, level):
            hits.append((name, level))
    for name, level in hits:
        send_telegram(
            f"🔔 Rupak Scanner Alert\nSymbol: {clean_symbol(symbol)}\n"
            f"Trigger: {name}\nPrice: {cur:.5f}\nLevel: {level:.5f}\n"
            f"Time: {ts}\nCheck chart before taking any trade."
        )
    return len(hits)


def main():
    symbols = read_symbols()
    print(f"Scanning {len(symbols)} symbols at {datetime.now(timezone.utc).isoformat()}")
    total=0; ok=0; errors=[]
    for s in symbols:
        try:
            n=scan_symbol(s); total+=n; ok+=1
            print(f"{s}: OK alerts={n}")
        except Exception as e:
            errors.append(f"{s}: {e}"); print(f"{s}: ERROR {e}")
    print(f"Done alerts={total}, errors={len(errors)}")
    if RUN_REASON == "workflow_dispatch":
        send_telegram(
            "✅ Rupak Cloud Scanner test complete\n"
            f"Symbols checked: {ok}/{len(symbols)}\n"
            "Active: PDH/PDL, QO, PYH/PYL, 15M SMA 20/50/200, H1 external swings (lookback 10)\n"
            f"Alerts this run: {total}\nErrors: {len(errors)}"
        )

if __name__ == "__main__":
    main()
