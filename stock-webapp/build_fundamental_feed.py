import json
from pathlib import Path

BASE = Path(__file__).resolve().parent
SRC = BASE / "data" / "extended.json"
OUT = BASE / "data" / "fundamentals.json"


def main():
    src = json.loads(SRC.read_text(encoding="utf-8"))
    data = {}
    for symbol, item in (src.get("data") or {}).items():
        if not isinstance(item, dict):
            continue
        data[symbol] = {"screen_metrics": item.get("screen_metrics") or {}}

    payload = {
        "updated_at": src.get("updated_at"),
        "source": src.get("source"),
        "fundamental_filter_fields": src.get("fundamental_filter_fields") or [],
        "data": data,
        "errors": src.get("errors") or [],
    }
    OUT.write_text(json.dumps(payload, indent=2, allow_nan=False), encoding="utf-8")
    print(f"Wrote slim fundamental feed for {len(data)} symbols")


if __name__ == "__main__":
    main()
