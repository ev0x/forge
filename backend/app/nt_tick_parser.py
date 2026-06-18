"""Parser for NinjaTrader tick data text exports.

NinjaTrader produces one file per (instrument, dataType), named e.g.
    FGBL 03-26.Last.txt
    MES 06-26.Last.txt

Line format (semicolon-delimited):
    YYYYMMDD HHMMSS XXXXXXX;<last>;<bid>;<ask>;<volume>

The trailing `XXXXXXX` after HHMMSS is a 7-digit fractional second in .NET
DateTime ticks (100-ns intervals; 10,000,000 per second). We round to ms.

Filename parsing: "FGBL 03-26.Last.txt" -> (root="FGBL", contract="FGBLH6")
where 03 -> H (March) and 26 -> '6' (last digit). This matches the existing
Sierra-style symbol convention used throughout the codebase.

We stream the file once, accumulate ticks into bars at every requested
timeframe simultaneously, and yield one (timeframe -> bars) dict at the end.
For a multi-million-tick file this is the only sane approach — re-parsing
per timeframe would be 9× the IO.
"""
from __future__ import annotations
import io
import os
import re
from dataclasses import dataclass
from datetime import datetime
from math import log10 as math_log10
from typing import Iterable, Iterator, Optional

# Futures month codes (Jan..Dec)
_MONTH_CODES = "FGHJKMNQUVXZ"
_FNAME_RE = re.compile(
    r'^(?P<root>[A-Z0-9]{1,5})\s+(?P<mm>\d{1,2})-(?P<yy>\d{2,4})(?:\.[A-Za-z]+)*\.txt$',
    re.IGNORECASE,
)


def parse_symbol_from_filename(filename: str) -> tuple[str, str]:
    """`FGBL 03-26.Last.txt` -> (root='FGBL', contract='FGBLH6').
    Returns (root, contract). If the filename doesn't match the expected
    pattern, contract defaults to root (so we can still store bars).
    """
    base = os.path.basename(filename)
    m = _FNAME_RE.match(base)
    if not m:
        # Fall back: use the whole basename minus the .txt
        stem = re.sub(r'\.txt$', '', base, flags=re.I)
        return stem, stem
    root = m.group('root').upper()
    month = int(m.group('mm'))
    yy = m.group('yy')
    if not (1 <= month <= 12):
        return root, root
    code = _MONTH_CODES[month - 1]
    year_short = yy[-1]   # last digit only (Sierra/NT convention: FGBLU6)
    return root, f"{root}{code}{year_short}"


@dataclass
class _BarAcc:
    """Mutable accumulator for one bar; finalised when the bucket ts changes."""
    o: float
    h: float
    l: float
    c: float
    v: float

    def update(self, price: float, vol: float) -> None:
        if price > self.h:
            self.h = price
        if price < self.l:
            self.l = price
        self.c = price
        self.v += vol


# Per-bar footprint: { price_key (rounded int) -> [bid_vol, ask_vol] }
# Stored as a plain dict for speed; keys are integer-quantised prices to avoid
# float-equality issues when grouping.


# Available timeframes (label -> seconds). Order matters for the UI.
TIMEFRAME_SECONDS: dict[str, int] = {
    "s30": 30,
    "m1": 60,
    "m2": 120,
    "m5": 300,
    "m15": 900,
    "m30": 1800,
    "h1": 3600,
    "h4": 14400,
    "d1": 86400,
}

# Footprint is only useful for short-horizon discretionary review; skip the
# coarsest timeframes to keep storage in check.
FOOTPRINT_TIMEFRAMES = {"s30", "m1", "m2", "m5", "m15", "m30", "h1"}

# Price-level quantisation. Per-instrument tick sizes — quantise to the
# instrument's tick to merge ticks that differ only in float-precision noise.
_TICK_BY_ROOT = {
    "MNQ": 0.25, "NQ": 0.25, "MES": 0.25, "ES": 0.25, "M2K": 0.10, "RTY": 0.10,
    "MYM": 1.0, "YM": 1.0,
    "GC": 0.10, "MGC": 0.10, "SI": 0.005, "SIL": 0.005,
    "CL": 0.01, "MCL": 0.01, "NG": 0.001,
    "FGBL": 0.01, "FGBM": 0.01, "FGBS": 0.005, "FGBX": 0.02,
}


def _tick_for_root(root: str) -> float:
    return _TICK_BY_ROOT.get((root or "").upper(), 0.01)


def _price_key(price: float, tick: float) -> int:
    """Quantise a float price into an integer key that maps cleanly to a tick
    grid. Avoids the "127.59 vs 127.5900000001" grouping bug."""
    return int(round(price / tick))


def _bucket_epoch(epoch_seconds: int, tf_seconds: int) -> int:
    return (epoch_seconds // tf_seconds) * tf_seconds


def _parse_line(line: str) -> Optional[tuple[int, float, float, float, float]]:
    """Parse one tick line. Returns (epoch_seconds_utc, last, bid, ask, volume) or None.

    Format: 'YYYYMMDD HHMMSS FRACTIONAL;last;bid;ask;volume'
    Bid/ask are needed to classify aggressor side for the footprint accumulator.
    """
    line = line.rstrip("\r\n")
    if not line:
        return None
    semi = line.find(';')
    if semi < 0:
        return None
    ts_part = line[:semi]
    rest = line[semi+1:]
    chunks = ts_part.split()
    if len(chunks) < 2:
        return None
    date_s, time_s = chunks[0], chunks[1]
    if len(date_s) != 8 or len(time_s) != 6:
        return None
    try:
        year = int(date_s[:4]); mo = int(date_s[4:6]); day = int(date_s[6:8])
        hh = int(time_s[:2]); mm = int(time_s[2:4]); ss = int(time_s[4:6])
        dt = datetime(year, mo, day, hh, mm, ss)
    except ValueError:
        return None
    fields = rest.split(';')
    if len(fields) < 4:
        return None
    try:
        last = float(fields[0])
        bid = float(fields[1]) if fields[1] else 0.0
        ask = float(fields[2]) if fields[2] else 0.0
        volume = float(fields[3])
    except ValueError:
        return None
    return int(dt.timestamp()), last, bid, ask, volume


def parse_nt_tick_to_bars(
    path: str,
    timeframes: Iterable[str] = TIMEFRAME_SECONDS.keys(),
) -> tuple[
    str, str,
    dict[str, list[tuple[datetime, float, float, float, float, float]]],
    dict[str, list[tuple[datetime, float, float, float]]],
    list[tuple[int, float, str, float]],
]:
    """Stream a NinjaTrader tick file once and aggregate to:
      - OHLCV bars per timeframe,
      - per-bar footprint levels (price, bid_vol, ask_vol) for FOOTPRINT_TIMEFRAMES,
      - the classified raw tick stream (ts_ms, price, side, size).

    Returns (root, contract, bars_by_tf, footprint_by_tf, ticks).
    ticks rows: (ts_ms, price, side ['A'|'B'], size).
    """
    root, contract = parse_symbol_from_filename(path)
    tick = _tick_for_root(root)
    tf_seconds = {tf: TIMEFRAME_SECONDS[tf] for tf in timeframes if tf in TIMEFRAME_SECONDS}

    accs: dict[str, dict[int, _BarAcc]] = {tf: {} for tf in tf_seconds}
    # footprint accumulator:
    #   fp[tf][bucket][price_key] = [bid_vol, ask_vol]
    fp: dict[str, dict[int, dict[int, list[float]]]] = {
        tf: {} for tf in tf_seconds if tf in FOOTPRINT_TIMEFRAMES
    }

    last_classification = 0  # +1 = aggressive buy, -1 = aggressive sell, 0 = unknown
    ticks: list[tuple[int, float, str, float]] = []

    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            parsed = _parse_line(line)
            if parsed is None:
                continue
            epoch, last, bid, ask, vol = parsed

            # Classify aggressor side. Standard rule:
            #   last >= ask          -> aggressive buy
            #   last <= bid          -> aggressive sell
            #   between (no spread)  -> carry the previous direction (last-tick rule)
            if ask > 0 and last >= ask - 1e-9:
                aggressor = 1
            elif bid > 0 and last <= bid + 1e-9:
                aggressor = -1
            elif bid > 0 and ask > 0 and ask > bid:
                # Strictly inside the spread — use mid as tiebreaker, fall back
                # to last classification if exactly at mid.
                mid = (bid + ask) / 2.0
                if last > mid: aggressor = 1
                elif last < mid: aggressor = -1
                else: aggressor = last_classification
            else:
                aggressor = last_classification
            if aggressor != 0:
                last_classification = aggressor

            # Append the raw classified tick. Use ms epoch (NT files don't carry
            # sub-second resolution at the second-level we parse; this is fine
            # for replay because the playhead runs at >= 60 ms granularity).
            side_letter = 'B' if aggressor > 0 else ('A' if aggressor < 0 else 'B')
            ticks.append((epoch * 1000, last, side_letter, vol))

            for tf, secs in tf_seconds.items():
                bucket = _bucket_epoch(epoch, secs)
                a = accs[tf].get(bucket)
                if a is None:
                    accs[tf][bucket] = _BarAcc(o=last, h=last, l=last, c=last, v=vol)
                else:
                    a.update(last, vol)
                # Footprint accumulation
                if tf in fp:
                    pk = _price_key(last, tick)
                    bar_fp = fp[tf].get(bucket)
                    if bar_fp is None:
                        bar_fp = {}
                        fp[tf][bucket] = bar_fp
                    lvl = bar_fp.get(pk)
                    if lvl is None:
                        lvl = [0.0, 0.0]
                        bar_fp[pk] = lvl
                    if aggressor > 0:
                        lvl[1] += vol      # ask-side (aggressive buy)
                    elif aggressor < 0:
                        lvl[0] += vol      # bid-side (aggressive sell)
                    else:
                        # Unknown: split half/half so volume isn't lost.
                        lvl[0] += vol / 2
                        lvl[1] += vol / 2

    bars_by_tf: dict[str, list[tuple[datetime, float, float, float, float, float]]] = {}
    for tf, by_bucket in accs.items():
        rows = []
        for bucket in sorted(by_bucket.keys()):
            a = by_bucket[bucket]
            rows.append((datetime.utcfromtimestamp(bucket), a.o, a.h, a.l, a.c, a.v))
        bars_by_tf[tf] = rows

    footprint_by_tf: dict[str, list[tuple[datetime, float, float, float]]] = {}
    # Decimal places that fully represent the tick grid (e.g. tick 0.01 -> 2dp).
    dec = max(0, -int(round(math_log10(tick)))) if tick > 0 else 4
    for tf, by_bucket in fp.items():
        rows = []
        for bucket in sorted(by_bucket.keys()):
            ts = datetime.utcfromtimestamp(bucket)
            for pk, (bv, av) in by_bucket[bucket].items():
                price = round(pk * tick, dec)
                rows.append((ts, price, bv, av))
        footprint_by_tf[tf] = rows

    return root, contract, bars_by_tf, footprint_by_tf, ticks
