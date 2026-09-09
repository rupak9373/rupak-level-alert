import csv
import io
import json
import time
from pathlib import Path
from datetime import datetime, timezone, date
from urllib.parse import quote_plus

import requests
from jugaad_data.nse import NSELive

BASE = Path(__file__).resolve().parent
SYMBOLS_FILE = BASE / "option_symbols.txt"
OUT = BASE / "data" / "option_chain.json"

# Official NSE report: eligible derivative underlyings + permitted market lots.
NSE_LOTS_URL = "https://nsearchives.nseindia.com/content/fo/fo_mktlots.csv"
INDEX_SYMBOLS = {"NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "NIFTYNXT50"}

# BSE index-option identifiers used by the BSE public derivatives endpoints.
BSE_INDEXES = {
    "SENSEX": 1,
    "BANKEX": 12,
    "SENSEX50": 47,
}

MAX_STRIKES_PER_EXPIRY = 81
HTTP_TIMEOUT = 15
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/134 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
}


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


def row_expiry(r):
    if not isinstance(r, dict):
        return None
    if r.get("expiryDate"):
        return r.get("expiryDate")
    for key in ("CE", "PE"):
        d = r.get(key)
        if isinstance(d, dict) and d.get("expiryDate"):
            return d.get("expiryDate")
    return None


def expiry_is_current_or_future(s):
    for fmt in ("%d-%b-%Y", "%d %b %Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(str(s).strip(), fmt).date() >= date.today()
        except Exception:
            pass
    return True


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


def load_previous():
    try:
        if OUT.exists():
            return json.loads(OUT.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def fallback_symbols():
    if not SYMBOLS_FILE.exists():
        return []
    return [
        x.strip().upper() for x in SYMBOLS_FILE.read_text(encoding="utf-8").splitlines()
        if x.strip() and not x.strip().startswith("#")
    ]


def nse_universe():
    """Return all currently eligible NSE F&O underlyings from the official lot-size report."""
    try:
        r = requests.get(NSE_LOTS_URL, headers=HEADERS, timeout=HTTP_TIMEOUT)
        r.raise_for_status()
        text = r.content.decode("utf-8-sig", errors="replace")
        reader = csv.DictReader(io.StringIO(text))
        found = {}
        for row in reader:
            if not row:
                continue
            clean = {str(k).strip().lower(): v for k, v in row.items()}
            symbol = None
            for k, v in clean.items():
                if "symbol" in k and v:
                    symbol = str(v).strip().upper()
                    break
            if not symbol:
                # Older report layouts may put the symbol in the first useful column.
                vals = [str(v).strip() for v in row.values() if v and str(v).strip()]
                symbol = vals[0].upper() if vals else None
            if not symbol or symbol in {"SYMBOL", "UNDERLYING"}:
                continue
            lot = None
            for k, v in clean.items():
                if "lot" in k:
                    lot = intnum(v)
                    if lot is not None:
                        break
            found[symbol] = lot
        if found:
            return found
    except Exception:
        pass
    return {s: None for s in fallback_symbols() if s not in BSE_INDEXES}


def fetch_nse_chain(live, symbol, lot_size=None):
    raw = live.index_option_chain(symbol) if symbol in INDEX_SYMBOLS else live.equities_option_chain(symbol)
    if not isinstance(raw, dict):
        raise ValueError("Invalid NSE option-chain response")
    records = raw.get("records") or {}
    rows = records.get("data") or raw.get("filtered", {}).get("data") or []
    expiries = records.get("expiryDates") or []
    underlying = num(records.get("underlyingValue"))

    grouped = {}
    for r in rows:
        if not isinstance(r, dict):
            continue
        expiry = row_expiry(r)
        if expiry:
            grouped.setdefault(expiry, []).append(r)

    chains = {}
    for expiry, all_rows in grouped.items():
        # PCR is calculated on the complete returned expiry before visual slicing.
        ce_oi_all = pe_oi_all = ce_vol_all = pe_vol_all = 0
        for r in all_rows:
            ce, pe = side(r.get("CE")), side(r.get("PE"))
            if ce:
                ce_oi_all += ce.get("oi") or 0
                ce_vol_all += ce.get("volume") or 0
            if pe:
                pe_oi_all += pe.get("oi") or 0
                pe_vol_all += pe.get("volume") or 0

        out = []
        for r in sorted(nearest_slice(all_rows, underlying), key=lambda x: num(x.get("strikePrice")) or 0):
            out.append({
                "strike": num(r.get("strikePrice")),
                "expiry": expiry,
                "ce": side(r.get("CE")),
                "pe": side(r.get("PE")),
            })
        chains[expiry] = {
            "rows": out,
            "pcr_oi": round(pe_oi_all / ce_oi_all, 3) if ce_oi_all else None,
            "pcr_volume": round(pe_vol_all / ce_vol_all, 3) if ce_vol_all else None,
        }

    usable = [e for e in expiries if e in chains and expiry_is_current_or_future(e)]
    if not usable:
        usable = [e for e in chains if expiry_is_current_or_future(e)] or list(chains)

    return {
        "symbol": symbol,
        "exchange": "NSE",
        "type": "index" if symbol in INDEX_SYMBOLS else "equity",
        "lot_size": lot_size,
        "underlying": underlying,
        "expiries": usable,
        "chains": chains,
        "timestamp": records.get("timestamp") or raw.get("filtered", {}).get("timestamp"),
    }


def bse_side(row, call=True):
    p = "C_" if call else ""
    return {
        "ltp": num(row.get(p + "Last_Trd_Price")),
        "change": num(row.get(p + "NetChange")),
        "pchange": num(row.get(p + "PctChange") or row.get(p + "PercentChange")),
        "oi": intnum(row.get(p + "Open_Interest")),
        "oi_change": intnum(row.get(p + "Absolute_Change_OI")),
        "oi_change_pct": num(row.get(p + "Percent_Change_OI")),
        "volume": intnum(row.get(p + "Vol_Traded")),
        "iv": num(row.get(p + "IV")),
        "bid_qty": intnum(row.get(p + "BIdQty")),
        "bid": num(row.get(p + "BidPrice")),
        "ask": num(row.get(p + "OfferPrice")),
        "ask_qty": intnum(row.get(p + "OfferQty")),
    }


def fetch_bse_chain(symbol, scrip_cd):
    sess = requests.Session()
    sess.headers.update({**HEADERS, "Origin": "https://www.bseindia.com", "Referer": "https://www.bseindia.com/"})
    try:
        sess.get("https://www.bseindia.com", timeout=HTTP_TIMEOUT)
    except Exception:
        pass

    exp_url = "https://api.bseindia.com/BseIndiaAPI/api/ddlExpiry/w"
    er = sess.get(exp_url, params={"ProductType": "IO", "scrip_cd": scrip_cd}, timeout=HTTP_TIMEOUT)
    er.raise_for_status()
    exprows = (er.json() or {}).get("Table") or []
    expiries = [str(x.get("eXPIRY") or "").strip() for x in exprows if x.get("eXPIRY")]
    expiries = [e for e in expiries if expiry_is_current_or_future(e)] or expiries

    chains = {}
    underlying = None
    timestamp = None
    for expiry in expiries:
        url = "https://api.bseindia.com/BseIndiaAPI/api/DerivOptionChain/w"
        rr = sess.get(url, params={"Expiry": expiry, "ProductType": "IO", "scrip_cd": scrip_cd}, timeout=HTTP_TIMEOUT)
        rr.raise_for_status()
        payload = rr.json() or {}
        table = payload.get("Table") or []
        if not table:
            continue
        if underlying is None:
            underlying = num(table[0].get("UlaValue"))
        if not timestamp:
            ason = payload.get("ASON") or []
            if ason and isinstance(ason[0], dict):
                timestamp = ason[0].get("DT_TM")

        ce_oi = pe_oi = ce_vol = pe_vol = 0
        raw_rows = []
        for r in table:
            ce, pe = bse_side(r, True), bse_side(r, False)
            ce_oi += ce.get("oi") or 0
            pe_oi += pe.get("oi") or 0
            ce_vol += ce.get("volume") or 0
            pe_vol += pe.get("volume") or 0
            raw_rows.append({
                "strike": num(r.get("Strike_Price")),
                "expiry": expiry,
                "ce": ce,
                "pe": pe,
            })
        raw_rows = [x for x in raw_rows if x["strike"] is not None]
        raw_rows.sort(key=lambda x: x["strike"])
        # Keep a broad ATM window for a responsive page while retaining full-expiry PCR.
        if underlying is not None and len(raw_rows) > MAX_STRIKES_PER_EXPIRY:
            center = min(range(len(raw_rows)), key=lambda i: abs(raw_rows[i]["strike"] - underlying))
            half = MAX_STRIKES_PER_EXPIRY // 2
            raw_rows = raw_rows[max(0, center-half): min(len(raw_rows), center+half+1)]
        chains[expiry] = {
            "rows": raw_rows,
            "pcr_oi": round(pe_oi / ce_oi, 3) if ce_oi else None,
            "pcr_volume": round(pe_vol / ce_vol, 3) if ce_vol else None,
        }
        time.sleep(0.08)

    if not chains:
        raise ValueError("No BSE option-chain rows returned")
    valid_expiries = [e for e in expiries if e in chains] or list(chains)
    return {
        "symbol": symbol,
        "exchange": "BSE",
        "type": "index",
        "lot_size": None,
        "underlying": underlying,
        "expiries": valid_expiries,
        "chains": chains,
        "timestamp": timestamp,
    }


def main():
    previous = load_previous()
    old_data = previous.get("data", {}) if isinstance(previous, dict) else {}
    live = NSELive()
    data, errors = {}, []

    universe = nse_universe()
    # Put indexes first, then all stock F&O symbols alphabetically.
    nse_symbols = sorted(universe, key=lambda s: (0 if s in INDEX_SYMBOLS else 1, s))

    for symbol in nse_symbols:
        try:
            data[symbol] = fetch_nse_chain(live, symbol, universe.get(symbol))
        except Exception as e:
            if symbol in old_data and (old_data[symbol].get("chains") or {}):
                data[symbol] = old_data[symbol]
            errors.append({"symbol": symbol, "exchange": "NSE", "error": str(e)})
        time.sleep(0.04)

    for symbol, code in BSE_INDEXES.items():
        try:
            data[symbol] = fetch_bse_chain(symbol, code)
        except Exception as e:
            if symbol in old_data and (old_data[symbol].get("chains") or {}):
                data[symbol] = old_data[symbol]
            errors.append({"symbol": symbol, "exchange": "BSE", "error": str(e)})

    if not data and old_data:
        data = old_data

    symbols = sorted(data, key=lambda s: (
        0 if data[s].get("exchange") == "NSE" and data[s].get("type") == "index" else
        1 if data[s].get("exchange") == "BSE" else 2,
        s,
    ))
    payload = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "source": "NSE India + BSE India public derivatives endpoints",
        "symbols": symbols,
        "counts": {
            "all": len(symbols),
            "nse": sum(1 for s in symbols if data[s].get("exchange") == "NSE"),
            "bse": sum(1 for s in symbols if data[s].get("exchange") == "BSE"),
            "indices": sum(1 for s in symbols if data[s].get("type") == "index"),
            "stocks": sum(1 for s in symbols if data[s].get("type") == "equity"),
        },
        "data": data,
        "errors": errors[-300:],
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, indent=2, allow_nan=False), encoding="utf-8")
    print(f"Wrote option chain for {len(data)} symbols; errors={len(errors)}")


if __name__ == "__main__":
    main()
