import json
from pathlib import Path
from datetime import date, timedelta, datetime, timezone

import pandas as pd
from jugaad_data.nse import NSELive, stock_df

BASE = Path(__file__).resolve().parent
SYMBOLS_FILE = BASE / "stock_symbols.txt"
OUT = BASE / "data" / "extended.json"
HISTORY_DAYS = 1000


def scalar_dict(obj):
    if not isinstance(obj, dict):
        return {}
    out = {}
    for k, v in obj.items():
        if isinstance(v, (str, int, float, bool)) or v is None:
            out[k] = v
    return out


def get_col(df, names):
    for name in names:
        if name in df.columns:
            return name
    return None


def history(symbol):
    end = date.today()
    start = end - timedelta(days=HISTORY_DAYS)
    df = stock_df(symbol=symbol, from_date=start, to_date=end, series="EQ")
    if df is None or df.empty:
        return pd.DataFrame()
    dc = get_col(df, ["CH_TIMESTAMP", "TIMESTAMP", "DATE"])
    oc = get_col(df, ["CH_OPENING_PRICE", "OPEN"])
    hc = get_col(df, ["CH_TRADE_HIGH_PRICE", "HIGH"])
    lc = get_col(df, ["CH_TRADE_LOW_PRICE", "LOW"])
    cc = get_col(df, ["CH_CLOSING_PRICE", "CLOSE"])
    vc = get_col(df, ["CH_TOT_TRADED_QTY", "TOTTRDQTY", "VOLUME"])
    if not all([dc, oc, hc, lc, cc]):
        return pd.DataFrame()
    work = df[[x for x in [dc, oc, hc, lc, cc, vc] if x]].copy()
    work[dc] = pd.to_datetime(work[dc], errors="coerce").dt.tz_localize(None)
    for c in [oc, hc, lc, cc]:
        work[c] = pd.to_numeric(work[c], errors="coerce")
    if vc:
        work[vc] = pd.to_numeric(work[vc], errors="coerce").fillna(0)
    else:
        work["_VOL"] = 0
        vc = "_VOL"
    work = work.dropna(subset=[dc, oc, hc, lc, cc]).sort_values(dc).drop_duplicates(dc, keep="last")
    work = work.rename(columns={dc:"date",oc:"open",hc:"high",lc:"low",cc:"close",vc:"volume"})
    return work[["date","open","high","low","close","volume"]].reset_index(drop=True)


def resample(df, rule):
    if df.empty:
        return df
    return (df.set_index("date").resample(rule).agg({
        "open":"first","high":"max","low":"min","close":"last","volume":"sum"
    }).dropna(subset=["open","high","low","close"]).reset_index())


def rows(df, limit):
    out = []
    for _, r in df.tail(limit).iterrows():
        out.append({
            "date": pd.Timestamp(r["date"]).strftime("%Y-%m-%d"),
            "open": round(float(r["open"]), 2),
            "high": round(float(r["high"]), 2),
            "low": round(float(r["low"]), 2),
            "close": round(float(r["close"]), 2),
            "volume": int(float(r["volume"])) if pd.notna(r["volume"]) else 0,
        })
    return out


def main():
    symbols = [x.strip().replace(".NS", "") for x in SYMBOLS_FILE.read_text(encoding="utf-8").splitlines() if x.strip() and not x.startswith("#")]
    live = NSELive()
    data, errors = {}, []
    for symbol in symbols:
        try:
            d = history(symbol)
            charts = {
                "1D": rows(d, 120),
                "1W": rows(resample(d, "W-FRI"), 104),
                "1M": rows(resample(d, "M"), 36),
            }
            try:
                q = live.stock_quote(symbol) or {}
            except Exception as e:
                q = {}
                errors.append({"symbol":symbol,"stage":"quote","error":str(e)})
            fundamentals = {
                "info": scalar_dict(q.get("info")),
                "metadata": scalar_dict(q.get("metadata")),
                "securityInfo": scalar_dict(q.get("securityInfo")),
                "industryInfo": scalar_dict(q.get("industryInfo")),
            }
            data[symbol] = {"charts": charts, "fundamentals": fundamentals}
        except Exception as e:
            errors.append({"symbol":symbol,"stage":"history","error":str(e)})
    payload = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "source": "NSE India",
        "chart_timeframes": ["1D","1W","1M"],
        "intraday_chart_status": "unavailable_without_reliable_historical_intraday_ohlc",
        "data": data,
        "errors": errors,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, indent=2, allow_nan=False), encoding="utf-8")
    print(f"Wrote extended data for {len(data)} symbols; errors={len(errors)}")


if __name__ == "__main__":
    main()
