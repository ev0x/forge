"""Futures contract specs. Sierra exports prices as integer ticks (price * divisor).
Add more roots as needed — the parser falls back to a default if unknown.

For non-USD-denominated instruments (e.g. Eurex FGBL), set `currency` and
`fx_rate_to_usd`. The matching layer multiplies gross PnL and per-fill
commissions by this rate so trade values land in USD even though the
underlying contract settles in another currency.

The user can override per-root FX rates without editing this file by setting
the env var `INSTRUMENT_FX_RATES_USD` to a JSON object, e.g.

    INSTRUMENT_FX_RATES_USD={"FGBL":1.162,"FOAT":1.155}
"""
import json
import os
import re

INSTRUMENT_SPECS = {
    "MNQ": {"price_divisor": 100.0, "point_value": 2.0, "tick_size": 0.25, "name": "Micro E-mini Nasdaq-100"},
    "MES": {"price_divisor": 100.0, "point_value": 5.0, "tick_size": 0.25, "name": "Micro E-mini S&P 500"},
    "M2K": {"price_divisor": 100.0, "point_value": 5.0, "tick_size": 0.10, "name": "Micro E-mini Russell 2000"},
    "MYM": {"price_divisor": 100.0, "point_value": 0.50, "tick_size": 1.0, "name": "Micro E-mini Dow"},
    "NQ": {"price_divisor": 100.0, "point_value": 20.0, "tick_size": 0.25, "name": "E-mini Nasdaq-100"},
    "ES": {"price_divisor": 100.0, "point_value": 50.0, "tick_size": 0.25, "name": "E-mini S&P 500"},
    "RTY": {"price_divisor": 100.0, "point_value": 50.0, "tick_size": 0.10, "name": "E-mini Russell 2000"},
    "YM": {"price_divisor": 100.0, "point_value": 5.0, "tick_size": 1.0, "name": "E-mini Dow"},
    "GC": {"price_divisor": 10.0, "point_value": 100.0, "tick_size": 0.10, "name": "Gold"},
    "MGC": {"price_divisor": 10.0, "point_value": 10.0, "tick_size": 0.10, "name": "Micro Gold"},
    "SI": {"price_divisor": 1000.0, "point_value": 5000.0, "tick_size": 0.005, "name": "Silver"},
    "SIL": {"price_divisor": 1000.0, "point_value": 1000.0, "tick_size": 0.005, "name": "Micro Silver"},
    "CL": {"price_divisor": 100.0, "point_value": 1000.0, "tick_size": 0.01, "name": "Crude Oil"},
    "MCL": {"price_divisor": 100.0, "point_value": 100.0, "tick_size": 0.01, "name": "Micro Crude Oil"},
    "NG": {"price_divisor": 1000.0, "point_value": 10000.0, "tick_size": 0.001, "name": "Natural Gas"},
    "MBT": {"price_divisor": 1.0, "point_value": 0.10, "tick_size": 5.0, "name": "Micro Bitcoin"},
    "MET": {"price_divisor": 100.0, "point_value": 0.10, "tick_size": 0.50, "name": "Micro Ether"},
    "6E": {"price_divisor": 10000.0, "point_value": 125000.0, "tick_size": 0.00005, "name": "Euro FX"},
    "M6E": {"price_divisor": 10000.0, "point_value": 12500.0, "tick_size": 0.0001, "name": "Micro Euro"},
    # Eurex Bund. Native contract is €1000/point with tick 0.01 = €10.
    # Brokers settle in USD by FX-converting the EUR PnL — handled via fx_rate_to_usd.
    # `exchange_fee_per_side` is Eurex's exchange/clearing fee charged on every
    # fill, in native currency. Tradovate's `commission` column doesn't include
    # it, so brokers' reported net is lower than what fills imply. ~€0.44/side
    # matches Tradeify FGBL broker statements as of 2026-06.
    "FGBL": {"price_divisor": 100.0, "point_value": 1000.0, "tick_size": 0.01,
             "name": "Euro Bund (10y)", "currency": "EUR", "fx_rate_to_usd": 1.162,
             "exchange_fee_per_side": 0.44},
}

DEFAULT_SPEC = {"price_divisor": 1.0, "point_value": 1.0, "tick_size": 0.01,
                "name": "Unknown", "currency": "USD", "fx_rate_to_usd": 1.0}


# Per-root FX overrides from env, applied once at module import. Lets the user
# retune EUR/USD (etc.) without a code change — edit .env, restart api.
_fx_env = os.environ.get("INSTRUMENT_FX_RATES_USD", "").strip()
if _fx_env:
    try:
        _overrides = json.loads(_fx_env)
        for _root, _rate in _overrides.items():
            if _root in INSTRUMENT_SPECS:
                INSTRUMENT_SPECS[_root]["fx_rate_to_usd"] = float(_rate)
    except (ValueError, TypeError) as e:
        print(f"[instruments] ignoring malformed INSTRUMENT_FX_RATES_USD: {e}")

_ROOT_RE = re.compile(r"^([A-Z0-9]{1,4}?)([FGHJKMNQUVXZ])(\d{1,2})(\..+)?$")


def extract_root(symbol: str) -> str:
    if not symbol:
        return ""
    base = symbol.split(".")[0]
    for root in sorted(INSTRUMENT_SPECS.keys(), key=len, reverse=True):
        if base.startswith(root) and len(base) >= len(root) + 2:
            rest = base[len(root):]
            if rest[0] in "FGHJKMNQUVXZ" and rest[1:].isdigit():
                return root
    m = _ROOT_RE.match(base)
    if m:
        return m.group(1)
    return base


def get_spec(symbol: str) -> dict:
    root = extract_root(symbol)
    spec = INSTRUMENT_SPECS.get(root, DEFAULT_SPEC).copy()
    spec["root"] = root
    # Backfill currency / FX / fee defaults so callers can rely on these keys.
    spec.setdefault("currency", "USD")
    spec.setdefault("fx_rate_to_usd", 1.0)
    spec.setdefault("exchange_fee_per_side", 0.0)
    return spec
