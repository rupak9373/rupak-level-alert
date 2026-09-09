import json
from pathlib import Path
from datetime import datetime, timezone

from jugaad_data.nse import NSELive

BASE = Path(__file__).resolve().parent
SYMBOLS_FILE = BASE / "option_symbols.txt"
OUT = BASE / "data" / "option_chain.json"
INDEX_SYMBOLS = {"NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "NIFTYNXT50"}
MAX_STRIKES_PER_EXPIRY = 41


def num(v, digits=2):
    try:
        if v is None or v == "":
            return None
        return round(float(v), digits)
    except (TypeError, ValueError):
        return None


def intnum(v):
    try:
        if v is None or v == "":
            return None
        return int(float(v))
    except (TypeError, ValueError):
        return None


def side(d):
    if not isinstance(d, dict):
        return None
    return {
        "ltp": num(d.get("lastPrice")),
        "change": num(d.get("change")),
        "pchange": num(d.get("pChange") if d.get("pChange") is not None else d.get("pchange")),
        "oi": intnum(d.get("openInterest")),
        "oi_change": intnum(d.get("changeinOpenInterest")),
        "oi_change_pct": num(d.get("pchangeinOpenInterest")),
        "volume": intnum(d.get("totalTradedVolume")),
        "iv": num(d.get("impliedVolatility")),
        "bid_qty": intnum(d.get("bidQty")),
        "bid": num(d.get("bidprice") if d.get("bidprice") is not None else d.get("bidPrice")),
        "ask": num(d.get("askPrice")),
        "ask_qty": intnum(d.get("askQty")),
    }


def nearest_slice(rows, underlying):
    if not rows:
        return rows
    strikes = sorted({num(r.get("strikePrice")) for r in rows if num(r.get("strikePrice")) is not None})
    if not strikes:
        return rows
    if underlying is None:
        center = len(strikes) // 2
    else:
        center = min(range(len(strikes)), key=lambda i: abs(strikes[i] - underlying))
    half = MAX_STRIKES_PER_EXPIRY // 2
    keep = set(strikes[max(0, center-half): min(len(strikes), center+half+1)])
    return [r for r in rows if num(r.get("strikePrice")) in keep]


def fetch_chain(live, symbol):
    raw = live.index_option_chain(symbol) if symbol in INDEX_SYMBOLS else live.equities_option_chain(symbol)
    if not isinstance(raw, dict):
        raise ValueError("Invalid option-chain response")
    records = raw.get("records") or {}
    rows = records.get("data") or raw.get("filtered", {}).get("data") or []
    expiries = records.get("expiryDates") or []
    underlying = num(records.get("underlyingValue"))

    grouped = {}
    for r in rows:
        if not isinstance(r, dict):
            continue
        expiry = r.get("expiryDate") or "Unknown"
        grouped.setdefault(expiry, []).append(r)

    chains = {}
    total_ce_oi = total_pe_oi = 0
    total_ce_vol = total_pe_vol = 0
    for expiry, erows in grouped.items():
        erows = nearest_slice(erows, underlying)
        out = []
        ce_oi = pe_oi = ce_vol = pe_vol = 0
        for r in sorted(erows, key=lambda x: num(x.get("strikePrice")) or 0):
            ce = side(r.get("CE"))
            pe = side(r.get("PE"))
            if ce:
                ce_oi += ce.get("oi") or 0
                ce_vol += ce.get("volume") or 0
            if pe:
                pe_oi += pe.get("oi") or 0
                pe_vol += pe.get("volume") or 0
            out.append({
                "strike": num(r.get("strikePrice")),
                "expiry": expiry,
                "ce": ce,
                "pe": pe,
            })
        total_ce_oi += ce_oi
        total_pe_oi += pe_oi
        total_ce_vol += ce_vol
        total_pe_vol += pe_vol
        chains[expiry] = {
            "rows": out,
            "pcr_oi": round(pe_oi / ce_oi, 3) if ce_oi else None,
            "pcr_volume": round(pe_vol / ce_vol, 3) if ce_vol else None,
        }

    return {
        "symbol": symbol,
        "type": "index" if symbol in INDEX_SYMBOLS else "equity",
        "underlying": underlying,
        "expiries": expiries or list(chains.keys()),
        "chains": chains,
        "pcr_oi_all": round(total_pe_oi / total_ce_oi, 3) if total_ce_oi else None,
        "pcr_volume_all": round(total_pe_vol / total_ce_vol, 3) if total_ce_vol else None,
        "timestamp": records.get("timestamp") or raw.get("filtered", {}).get("timestamp"),
    }


def main():
    symbols = [
        x.strip().upper() for x in SYMBOLS_FILE.read_text(encoding="utf-8").splitlines()
        if x.strip() and not x.strip().startswith("#")
    ]
    live = NSELive()
    data, errors = {}, []
    for symbol in symbols:
        try:
            data[symbol] = fetch_chain(live, symbol)
        except Exception as e:
            errors.append({"symbol": symbol, "error": str(e)})

    payload = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "source": "NSE India",
        "symbols": list(data.keys()),
        "data": data,
        "errors": errors,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, indent=2, allow_nan=False), encoding="utf-8")
    print(f"Wrote option chain for {len(data)} symbols; errors={len(errors)}")


if __name__ == "__main__":
    main()
