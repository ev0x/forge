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


def _bucket_epoch(epoch_seconds: int, tf_seconds: int) -> int:
    return (epoch_seconds // tf_seconds) * tf_seconds


def _parse_line(line: str) -> Optional[tuple[int, float, float]]:
    """Parse one tick line. Returns (epoch_seconds_utc, price, volume) or None.

    Format: 'YYYYMMDD HHMMSS FRACTIONAL;price;bid;ask;volume'
    The fractional second is dropped (we bar at >= second resolution).
    """
    line = line.rstrip("\r\n")
    if not line:
        return None
    semi = line.find(';')
    if semi < 0:
        return None
    ts_part = line[:semi]
    rest = line[semi+1:]
    # Time prefix: 'YYYYMMDD HHMMSS FRAC'
    # Take first two space-separated chunks for date + time. The fractional
    # piece is optional in some exports.
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
    # rest: 'price;bid;ask;volume'
    fields = rest.split(';')
    if len(fields) < 4:
        return None
    try:
        price = float(fields[0])
        volume = float(fields[3])
    except ValueError:
        return None
    return int(dt.timestamp()), price, volume


def parse_nt_tick_to_bars(
    path: str,
    timeframes: Iterable[str] = TIMEFRAME_SECONDS.keys(),
) -> tuple[str, str, dict[str, list[tuple[datetime, float, float, float, float, float]]]]:
    """Stream a NinjaTrader tick file, aggregate to bars at each timeframe.

    Returns (root, contract_symbol, bars_by_tf) where each entry is a list of
    tuples (ts, o, h, l, c, v) ordered by ts.
    """
    root, contract = parse_symbol_from_filename(path)
    tf_seconds = {tf: TIMEFRAME_SECONDS[tf] for tf in timeframes if tf in TIMEFRAME_SECONDS}

    # Per-timeframe: { bucket_epoch -> _BarAcc }. Using a dict (not sorted list) is
    # O(1) per tick; final sort runs once at the end.
    accs: dict[str, dict[int, _BarAcc]] = {tf: {} for tf in tf_seconds}

    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            parsed = _parse_line(line)
            if parsed is None:
                continue
            epoch, price, vol = parsed
            for tf, secs in tf_seconds.items():
                bucket = _bucket_epoch(epoch, secs)
                a = accs[tf].get(bucket)
                if a is None:
                    accs[tf][bucket] = _BarAcc(o=price, h=price, l=price, c=price, v=vol)
                else:
                    a.update(price, vol)

    bars_by_tf: dict[str, list[tuple[datetime, float, float, float, float, float]]] = {}
    for tf, by_bucket in accs.items():
        rows = []
        for bucket in sorted(by_bucket.keys()):
            a = by_bucket[bucket]
            rows.append((datetime.utcfromtimestamp(bucket), a.o, a.h, a.l, a.c, a.v))
        bars_by_tf[tf] = rows
    return root, contract, bars_by_tf
