"""Parser for Tradovate's web app fill export.

Tradovate exposes a CSV from Reports → Fills. Sample header:
  _id,_orderId,_contractId,_timestamp,_tradeDate,_action,_qty,_price,_active,
  _accountId,Fill ID,Order ID,Timestamp,Date,Account,B/S,Quantity,Price,
  _priceFormat,_priceFormatType,_tickSize,Contract,Product,Product Description,
  commission

Every row is an actual fill (no Cancels / Rejects to skip — those live in the
order-history export). `_action` is the canonical side (0=Buy, 1=Sell), with the
text `B/S` column as redundant safety. `commission` is per-fill USD.

Tradovate's `Account` value is just the raw firm account id (e.g. TDFYG…). To
match the existing accounts that came in via NinjaTrader's `Connection-Account`
convention, we prefix any TDFY id with `tradeify-`. Same for known Apex /
Lucid prefixes — extend the map below when a new firm shows up.
"""
from __future__ import annotations
import csv
import re
from datetime import datetime
from typing import Iterator, Optional

from .parser import ParsedFill
from .instruments import get_spec


# Default exchange suffix per root, picked to match what NinjaTrader emits so
# Tradovate-imported fills aggregate cleanly with existing NT data.
_EXCHANGE_BY_ROOT = {
    "MNQ": "CME", "MES": "CME", "M2K": "CME", "MYM": "CME",
    "NQ": "CME", "ES": "CME", "YM": "CME", "RTY": "CME",
    "GC": "COMEX", "MGC": "COMEX", "SI": "COMEX", "SIL": "COMEX",
    "CL": "NYMEX", "MCL": "NYMEX", "NG": "NYMEX",
    "MBT": "CME", "MET": "CME",
    "6E": "CME", "M6E": "CME",
    # Eurex Bund family lives on Eurex but NT defaulted them to CME — keep
    # matching that so existing FGBLM6.CME rows aggregate with new imports.
    "FGBL": "CME", "FGBM": "CME", "FGBS": "CME", "FGBX": "CME",
}

# Root-extraction regex: 1–4 alphanumeric chars, then a month code, then a year digit.
_ROOT_FROM_CONTRACT = re.compile(r"^([A-Z0-9]{1,4}?)([FGHJKMNQUVXZ])(\d{1,2})$")


def _normalize_contract(contract: str) -> str:
    """`FGBLM6` -> `FGBLM6.CME` so it matches the symbol format NT writes."""
    if not contract:
        return contract
    c = contract.strip().upper()
    if "." in c:
        return c  # already qualified
    m = _ROOT_FROM_CONTRACT.match(c)
    if not m:
        return c
    root = m.group(1)
    exchange = _EXCHANGE_BY_ROOT.get(root, "CME")
    return f"{c}.{exchange}"


def _account_external_id(raw: str) -> str:
    """Map Tradovate's raw account id to the project's stored external_id form."""
    a = (raw or "").strip()
    if not a:
        return "tradovate-unknown"
    upper = a.upper()
    # Tradeify accounts: NT created them as "tradeify-TDFY…". Merge.
    if upper.startswith("TDFY"):
        return f"tradeify-{a}"
    # Apex / TopStep / TPT / Lucid: their raw account ids already carry the
    # firm prefix, so pass through unchanged.
    return a


def _parse_ts(s: str) -> Optional[datetime]:
    """Tradovate writes two timestamps per row. `_timestamp` is UTC ISO with
    millis (`2026-05-29 08:27:59.780Z`); `Timestamp` is local 24h
    (`05/29/2026 17:57:59`). We prefer `_timestamp` (timezone-stable)."""
    s = (s or "").strip()
    if not s:
        return None
    s = s.rstrip("Z").strip()
    for fmt in (
        "%Y-%m-%d %H:%M:%S.%f",
        "%Y-%m-%dT%H:%M:%S.%f",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%dT%H:%M:%S",
        "%m/%d/%Y %H:%M:%S",
        "%d/%m/%Y %H:%M:%S",
    ):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def looks_like_tradovate_fills_csv(text: str) -> bool:
    """Header-sniffer. Tradovate fills carry both `_id` and `Fill ID` columns
    plus `commission` — that triad is unique enough to distinguish from NT/Sierra."""
    head = text[:1500].splitlines()[:1]
    if not head:
        return False
    h = head[0].lower()
    return "_id" in h and "fill id" in h and "commission" in h and "contract" in h


def parse_tradovate_fills(file_path: str) -> Iterator[ParsedFill]:
    with open(file_path, "r", encoding="utf-8-sig", errors="replace", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            contract = (row.get("Contract") or "").strip()
            if not contract:
                continue
            symbol = _normalize_contract(contract)
            spec = get_spec(symbol)

            # Action: prefer numeric `_action` (0=Buy, 1=Sell); fall back to text `B/S`.
            action_raw = (row.get("_action") or "").strip()
            bs_raw = (row.get("B/S") or "").strip().lower()
            if action_raw == "0":
                side = "Buy"
            elif action_raw == "1":
                side = "Sell"
            elif bs_raw.startswith("buy"):
                side = "Buy"
            elif bs_raw.startswith("sell"):
                side = "Sell"
            else:
                continue

            try:
                qty = int(float(row.get("Quantity") or row.get("_qty") or "0"))
            except ValueError:
                continue
            if qty <= 0:
                continue

            try:
                price = float(row.get("Price") or row.get("_price") or "0")
            except ValueError:
                continue
            if price <= 0:
                continue

            ts = _parse_ts(row.get("_timestamp") or "") or _parse_ts(row.get("Timestamp") or "")
            if ts is None:
                continue

            fill_id = (row.get("Fill ID") or row.get("_id") or "").strip()
            order_id = (row.get("Order ID") or row.get("_orderId") or "").strip() or fill_id

            try:
                commission = float(row.get("commission") or "0")
            except ValueError:
                commission = 0.0

            acct_ext = _account_external_id(row.get("Account") or "")

            yield ParsedFill(
                fill_id=fill_id or f"tv-{ts.isoformat()}-{symbol}-{qty}-{price}",
                internal_order_id=order_id,
                service_order_id=fill_id,
                parent_internal_order_id=None,
                fill_time=ts,
                symbol=symbol,
                instrument_root=spec["root"],
                side=side,
                quantity=qty,
                fill_price=price,
                raw_price=price,
                order_type=None,
                open_close=None,  # Tradovate fills don't carry an open/close marker
                account_external_id=acct_ext,
                position_after=None,
                high_during_position=None,
                low_during_position=None,
                note=None,
                is_automated=False,
                commission=commission if commission > 0 else None,
            )
