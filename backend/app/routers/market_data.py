"""Market-data endpoints. Currently sources OHLC bars from NinjaTrader tick
text exports (.txt files from NT "Historical Data Manager → Export"). Tick
files get streamed once and aggregated into bars at every supported timeframe.

Bar lookup for a trade chart uses **root-prefix matching**: a trade on
`FGBLU6.CME` will match any bar with symbol starting with `FGBL` so the chart
finds data across all contract months without exact-symbol gymnastics.
"""
from datetime import datetime
from typing import Optional
from fastapi import APIRouter, Depends, File, UploadFile, HTTPException, Form, Query
from sqlalchemy.orm import Session
from sqlalchemy import select, func, or_
from sqlalchemy.dialects.postgresql import insert as pg_insert
import os
import re
import tempfile

from .. import models, schemas
from ..db import get_db
from ..nt_tick_parser import parse_nt_tick_to_bars, parse_symbol_from_filename, TIMEFRAME_SECONDS
from ..instruments import extract_root, get_spec

router = APIRouter(prefix="/api/market-data", tags=["market-data"])


@router.get("/timeframes")
def list_timeframes():
    """Frontend uses this to keep its picker in sync with backend support."""
    return [{"label": k, "seconds": v} for k, v in TIMEFRAME_SECONDS.items()]


@router.post("/upload-nt-tick", response_model=schemas.MarketDataUploadResult)
async def upload_nt_tick(
    file: UploadFile = File(...),
    symbol_override: Optional[str] = Form(None),
    db: Session = Depends(get_db),
):
    """Upload a NinjaTrader tick .txt file. The symbol is parsed from the
    filename (e.g. `FGBL 03-26.Last.txt` -> contract `FGBLH6`) unless overridden.

    All supported timeframes are aggregated on the same pass.
    """
    filename = file.filename or "unknown.txt"
    # Stream to a temp file so we can re-read it (the parser is iterator-based).
    tmp = tempfile.NamedTemporaryFile(prefix="nt_tick_", suffix=".txt", delete=False)
    tmp_path = tmp.name
    bytes_received = 0
    try:
        while True:
            chunk = await file.read(1024 * 1024)
            if not chunk:
                break
            tmp.write(chunk)
            bytes_received += len(chunk)
        tmp.close()
        if bytes_received == 0:
            raise HTTPException(400, "Empty file")
        if symbol_override:
            root = extract_root(symbol_override) or symbol_override
            contract = symbol_override.split(".")[0]
            _, _, bars_by_tf, footprint_by_tf, ticks = parse_nt_tick_to_bars(_with_name(tmp_path, filename))
        else:
            root, contract, bars_by_tf, footprint_by_tf, ticks = parse_nt_tick_to_bars(_with_name(tmp_path, filename))

        total_inserted = 0
        total_skipped = 0
        earliest: Optional[datetime] = None
        latest: Optional[datetime] = None
        timeframes_written: list[str] = []

        # Postgres caps prepared-statement params at 65535. Each row uses 9
        # columns → max 7281 rows per INSERT. Chunk well under that.
        CHUNK = 5000
        for tf, rows in bars_by_tf.items():
            if not rows:
                continue
            tf_inserted = 0
            for i in range(0, len(rows), CHUNK):
                chunk = rows[i:i+CHUNK]
                batch = [{
                    "symbol": contract, "timeframe": tf, "ts": ts,
                    "o": o, "h": h, "l": l, "c": c, "v": v,
                    "source": "nt_tick",
                } for (ts, o, h, l, c, v) in chunk]
                stmt = (pg_insert(models.MarketDataBar)
                        .values(batch)
                        .on_conflict_do_nothing(index_elements=["symbol", "timeframe", "ts"])
                        .returning(models.MarketDataBar.id))
                result = db.execute(stmt)
                tf_inserted += len(result.fetchall())
            total_inserted += tf_inserted
            total_skipped += len(rows) - tf_inserted
            tf_min = rows[0][0]
            tf_max = rows[-1][0]
            if earliest is None or tf_min < earliest:
                earliest = tf_min
            if latest is None or tf_max > latest:
                latest = tf_max
            timeframes_written.append(f"{tf}:{len(rows)}")

        # Raw ticks — for tick-by-tick footprint replay. Wipe any existing
        # ticks for this contract first (re-uploads of the same NT file are
        # idempotent — replay would otherwise see doubled volume).
        if ticks:
            db.query(models.MarketTick).filter_by(symbol=contract).delete()
            for i in range(0, len(ticks), CHUNK):
                chunk = ticks[i:i+CHUNK]
                batch = [{
                    "symbol": contract, "ts_ms": t, "price": p, "side": s, "size": v,
                } for (t, p, s, v) in chunk]
                db.execute(pg_insert(models.MarketTick).values(batch))

        # Footprint levels — written for the timeframes the parser flagged. Same
        # chunked-insert pattern; the table has its own (symbol, tf, ts, price)
        # unique key so re-uploads dedupe automatically.
        footprint_written = 0
        for tf, fp_rows in footprint_by_tf.items():
            if not fp_rows:
                continue
            for i in range(0, len(fp_rows), CHUNK):
                chunk = fp_rows[i:i+CHUNK]
                batch = [{
                    "symbol": contract, "timeframe": tf, "bar_ts": ts,
                    "price": price, "bid_volume": bv, "ask_volume": av,
                } for (ts, price, bv, av) in chunk]
                stmt = (pg_insert(models.FootprintLevel)
                        .values(batch)
                        .on_conflict_do_nothing(index_elements=["symbol", "timeframe", "bar_ts", "price"]))
                db.execute(stmt)
            footprint_written += len(fp_rows)
        db.commit()

        notes = [
            f"Source file: {filename} ({bytes_received/1024/1024:.1f} MB)",
            f"Symbol parsed: root={root}, contract={contract}",
            f"Aggregated to {len(bars_by_tf)} timeframes ({', '.join(timeframes_written)})",
            f"Footprint levels written: {footprint_written:,}",
        ]
        if symbol_override:
            notes.append(f"Symbol override applied: {symbol_override}")

        return schemas.MarketDataUploadResult(
            symbol=contract, timeframe="multi",
            parsed=sum(len(rows) for rows in bars_by_tf.values()),
            inserted=total_inserted, skipped_duplicates=total_skipped,
            price_divisor=1.0, earliest=earliest, latest=latest, notes=notes,
        )
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


def _with_name(path: str, original_name: str) -> str:
    """Symlink/alias trick: the parser keys symbol extraction off the basename,
    not the temp path. Copy the basename onto the temp file path by creating
    a hardlink in the same dir with the real filename. Falls back to bare path
    if linking fails (parser will then fall back to filename-only stem)."""
    safe_name = os.path.basename(original_name).replace("/", "_").replace("\\", "_")
    link_path = os.path.join(os.path.dirname(path), safe_name)
    try:
        if not os.path.exists(link_path):
            os.link(path, link_path)
        return link_path
    except OSError:
        return path


@router.get("/summary", response_model=list[schemas.MarketDataSummaryRow])
def summary(db: Session = Depends(get_db)):
    rows = db.execute(
        select(
            models.MarketDataBar.symbol,
            models.MarketDataBar.timeframe,
            func.count(models.MarketDataBar.id).label("n"),
            func.min(models.MarketDataBar.ts).label("earliest"),
            func.max(models.MarketDataBar.ts).label("latest"),
        ).group_by(models.MarketDataBar.symbol, models.MarketDataBar.timeframe)
        .order_by(models.MarketDataBar.symbol, models.MarketDataBar.timeframe)
    ).all()
    out = []
    for r in rows:
        sources = [s for (s,) in db.execute(
            select(models.MarketDataBar.source).where(
                models.MarketDataBar.symbol == r.symbol,
                models.MarketDataBar.timeframe == r.timeframe,
            ).distinct()
        ).all() if s]
        out.append(schemas.MarketDataSummaryRow(
            symbol=r.symbol, timeframe=r.timeframe, bar_count=r.n,
            earliest=r.earliest, latest=r.latest, sources=sources,
        ))
    return out


@router.get("/bars", response_model=list[schemas.MarketDataBarOut])
def get_bars(
    symbol: str,
    from_dt: str = Query(..., alias="from"),
    to_dt: str = Query(..., alias="to"),
    timeframe: str = "m5",
    db: Session = Depends(get_db),
):
    """Return bars for the trade chart. Matching is intentionally loose:
    - first try an exact symbol match (after stripping the exchange suffix);
    - else fall back to **root prefix** (`FGBLU6.CME` -> any symbol starting
      with `FGBL`).
    This lets a trade on a specific contract month draw on tick data uploaded
    under a slightly different naming convention.
    """
    try:
        start = datetime.fromisoformat(from_dt)
        end = datetime.fromisoformat(to_dt)
    except ValueError:
        raise HTTPException(400, "Bad ISO datetime in from/to")

    stripped = symbol.split(".")[0]  # 'FGBLU6.CME' -> 'FGBLU6'
    root = extract_root(symbol) or stripped

    # 1) Exact match on the stripped symbol
    rows = db.execute(
        select(models.MarketDataBar).where(
            models.MarketDataBar.symbol == stripped,
            models.MarketDataBar.timeframe == timeframe,
            models.MarketDataBar.ts >= start,
            models.MarketDataBar.ts <= end,
        ).order_by(models.MarketDataBar.ts)
    ).scalars().all()
    if rows:
        return rows

    # 2) Root-prefix fallback. Take the contract whose bars overlap the trade
    # window most (highest bar count); fall back to all-matching bars otherwise.
    candidates = db.execute(
        select(models.MarketDataBar.symbol, func.count(models.MarketDataBar.id))
        .where(
            models.MarketDataBar.symbol.like(f"{root}%"),
            models.MarketDataBar.timeframe == timeframe,
            models.MarketDataBar.ts >= start,
            models.MarketDataBar.ts <= end,
        ).group_by(models.MarketDataBar.symbol)
        .order_by(func.count(models.MarketDataBar.id).desc())
    ).all()
    if not candidates:
        return []
    best_symbol = candidates[0][0]
    rows = db.execute(
        select(models.MarketDataBar).where(
            models.MarketDataBar.symbol == best_symbol,
            models.MarketDataBar.timeframe == timeframe,
            models.MarketDataBar.ts >= start,
            models.MarketDataBar.ts <= end,
        ).order_by(models.MarketDataBar.ts)
    ).scalars().all()
    return rows


@router.get("/footprint")
def get_footprint(
    symbol: str,
    from_dt: str = Query(..., alias="from"),
    to_dt: str = Query(..., alias="to"),
    timeframe: str = "m5",
    db: Session = Depends(get_db),
):
    """Footprint levels in the requested window. Uses the same root-prefix
    matching as /bars so a trade on FGBLU6.CME picks up FGBLU6's footprint.
    Response: { bars: [{ts, levels: [{price, bid, ask}]}] }
    """
    try:
        start = datetime.fromisoformat(from_dt)
        end = datetime.fromisoformat(to_dt)
    except ValueError:
        raise HTTPException(400, "Bad ISO datetime in from/to")

    stripped = symbol.split(".")[0]
    root = extract_root(symbol) or stripped

    def fetch(sym: str):
        return db.execute(
            select(
                models.FootprintLevel.bar_ts,
                models.FootprintLevel.price,
                models.FootprintLevel.bid_volume,
                models.FootprintLevel.ask_volume,
            ).where(
                models.FootprintLevel.symbol == sym,
                models.FootprintLevel.timeframe == timeframe,
                models.FootprintLevel.bar_ts >= start,
                models.FootprintLevel.bar_ts <= end,
            ).order_by(models.FootprintLevel.bar_ts, models.FootprintLevel.price)
        ).all()

    rows = fetch(stripped)
    if not rows:
        # Pick the contract with the most footprint coverage in this window.
        candidates = db.execute(
            select(models.FootprintLevel.symbol, func.count(models.FootprintLevel.id))
            .where(
                models.FootprintLevel.symbol.like(f"{root}%"),
                models.FootprintLevel.timeframe == timeframe,
                models.FootprintLevel.bar_ts >= start,
                models.FootprintLevel.bar_ts <= end,
            ).group_by(models.FootprintLevel.symbol)
            .order_by(func.count(models.FootprintLevel.id).desc())
        ).all()
        if not candidates:
            return {"bars": []}
        rows = fetch(candidates[0][0])

    # Group rows by bar_ts
    bars: list[dict] = []
    current_ts = None
    current_levels: list[dict] = []
    for ts, price, bid, ask in rows:
        if ts != current_ts:
            if current_ts is not None:
                bars.append({"ts": current_ts.isoformat(), "levels": current_levels})
            current_ts = ts
            current_levels = []
        current_levels.append({"price": price, "bid": bid, "ask": ask})
    if current_ts is not None:
        bars.append({"ts": current_ts.isoformat(), "levels": current_levels})
    return {"bars": bars}


@router.get("/footprint-ticks")
def get_footprint_ticks(
    symbol: str,
    from_dt: str = Query(..., alias="from"),
    to_dt: str = Query(..., alias="to"),
    bar_seconds: int = 300,
    trade_id: Optional[int] = None,
    db: Session = Depends(get_db),
):
    """Tick-level footprint data for true tick-by-tick replay. Returns the
    raw classified ticks in the window plus precomputed bar/price axes so the
    frontend can aggregate on-the-fly without the axes shifting.

    Same root-prefix matching as /bars: a trade on FGBLU6.CME will find
    FGBL%-prefixed tick data and pick the contract with the most coverage.
    """
    try:
        start = datetime.fromisoformat(from_dt)
        end = datetime.fromisoformat(to_dt)
    except ValueError:
        raise HTTPException(400, "Bad ISO datetime in from/to")

    start_ms = int(start.timestamp() * 1000)
    end_ms = int(end.timestamp() * 1000)

    stripped = symbol.split(".")[0]
    root = extract_root(symbol) or stripped

    def fetch_ticks(sym: str):
        return db.execute(
            select(models.MarketTick.ts_ms, models.MarketTick.price,
                   models.MarketTick.side, models.MarketTick.size)
            .where(
                models.MarketTick.symbol == sym,
                models.MarketTick.ts_ms >= start_ms,
                models.MarketTick.ts_ms <= end_ms,
            ).order_by(models.MarketTick.ts_ms)
        ).all()

    rows = fetch_ticks(stripped)
    matched_symbol = stripped
    if not rows:
        candidates = db.execute(
            select(models.MarketTick.symbol, func.count(models.MarketTick.id))
            .where(
                models.MarketTick.symbol.like(f"{root}%"),
                models.MarketTick.ts_ms >= start_ms,
                models.MarketTick.ts_ms <= end_ms,
            ).group_by(models.MarketTick.symbol)
            .order_by(func.count(models.MarketTick.id).desc())
        ).all()
        if candidates:
            matched_symbol = candidates[0][0]
            rows = fetch_ticks(matched_symbol)

    # Bar axis: bucketed bar start times in ms.
    bar_ms = bar_seconds * 1000
    bar_set: set[int] = set()
    price_set: set[float] = set()
    tick_size = 0.01
    # Use the instrument spec's tick if we have it, else infer from data.
    spec = get_spec(matched_symbol)
    tick_size = spec.get("tick_size", 0.01)

    ticks: list[dict] = []
    for t, p, s, v in rows:
        bucket = (t // bar_ms) * bar_ms
        bar_set.add(bucket)
        price_set.add(round(p, 5))
        ticks.append({"t": t, "price": p, "side": s, "size": v})

    bars = [{"ts": ts} for ts in sorted(bar_set)]
    prices = sorted(price_set, reverse=True)

    # Trade markers
    markers: dict = {"direction": None, "entry": None, "exit": None}
    if trade_id is not None:
        trade = db.get(models.Trade, trade_id)
        if trade:
            markers["direction"] = "SHORT" if trade.side == "Short" else "LONG"
            if trade.entry_time:
                markers["entry"] = {
                    "ts": int(trade.entry_time.timestamp() * 1000),
                    "price": trade.avg_entry_price,
                }
            if trade.exit_time:
                markers["exit"] = {
                    "ts": int(trade.exit_time.timestamp() * 1000),
                    "price": trade.avg_exit_price,
                }

    return {
        "cached": True,
        "symbol": matched_symbol,
        "bars": bars,
        "prices": prices,
        "ticks": ticks,
        "bar_seconds": bar_seconds,
        "tick_size": tick_size,
        "tick_count": len(ticks),
        "markers": markers,
        "cost": 0.0,
    }


@router.delete("/symbol/{symbol}")
def delete_symbol(symbol: str, timeframe: Optional[str] = None, db: Session = Depends(get_db)):
    q = db.query(models.MarketDataBar).filter_by(symbol=symbol)
    fq = db.query(models.FootprintLevel).filter_by(symbol=symbol)
    if timeframe:
        q = q.filter_by(timeframe=timeframe)
        fq = fq.filter_by(timeframe=timeframe)
    n = q.count(); q.delete()
    fn = fq.count(); fq.delete()
    db.commit()
    return {"deleted": n, "footprint_deleted": fn}


@router.delete("/all")
def delete_all(db: Session = Depends(get_db)):
    """Wipe every cached bar + footprint level + raw tick. Used when switching data sources."""
    n = db.query(models.MarketDataBar).count()
    db.query(models.MarketDataBar).delete()
    fn = db.query(models.FootprintLevel).count()
    db.query(models.FootprintLevel).delete()
    tn = db.query(models.MarketTick).count()
    db.query(models.MarketTick).delete()
    db.commit()
    return {"deleted": n, "footprint_deleted": fn, "ticks_deleted": tn}


# --- Symbol normalization helper exposed for the frontend ----------------
_CONTRACT_RE = re.compile(r'^([A-Z0-9]{1,4}?)([FGHJKMNQUVXZ])(\d{1,2})(?:\.[A-Z]+)?$')


@router.get("/symbol-for-trade")
def symbol_for_trade(trade_symbol: str):
    """Given a trade.symbol (e.g. 'FGBLU6.CME'), return what symbol the chart
    will end up using based on what's in the DB. Diagnostic-only."""
    stripped = trade_symbol.split(".")[0]
    root = extract_root(trade_symbol) or stripped
    return {"trade_symbol": trade_symbol, "stripped": stripped, "root": root}
