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
from ..instruments import extract_root

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
            # Still aggregate using the parser's path-only logic, but override
            # what we *store* the bars under.
            _, _, bars_by_tf = parse_nt_tick_to_bars(_with_name(tmp_path, filename))
        else:
            root, contract, bars_by_tf = parse_nt_tick_to_bars(_with_name(tmp_path, filename))

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
        db.commit()

        notes = [
            f"Source file: {filename} ({bytes_received/1024/1024:.1f} MB)",
            f"Symbol parsed: root={root}, contract={contract}",
            f"Aggregated to {len(bars_by_tf)} timeframes ({', '.join(timeframes_written)})",
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


@router.delete("/symbol/{symbol}")
def delete_symbol(symbol: str, timeframe: Optional[str] = None, db: Session = Depends(get_db)):
    q = db.query(models.MarketDataBar).filter_by(symbol=symbol)
    if timeframe:
        q = q.filter_by(timeframe=timeframe)
    n = q.count(); q.delete(); db.commit()
    return {"deleted": n}


@router.delete("/all")
def delete_all(db: Session = Depends(get_db)):
    """Wipe every cached bar. Used when switching data sources."""
    n = db.query(models.MarketDataBar).count()
    db.query(models.MarketDataBar).delete()
    db.commit()
    return {"deleted": n}


# --- Symbol normalization helper exposed for the frontend ----------------
_CONTRACT_RE = re.compile(r'^([A-Z0-9]{1,4}?)([FGHJKMNQUVXZ])(\d{1,2})(?:\.[A-Z]+)?$')


@router.get("/symbol-for-trade")
def symbol_for_trade(trade_symbol: str):
    """Given a trade.symbol (e.g. 'FGBLU6.CME'), return what symbol the chart
    will end up using based on what's in the DB. Diagnostic-only."""
    stripped = trade_symbol.split(".")[0]
    root = extract_root(trade_symbol) or stripped
    return {"trade_symbol": trade_symbol, "stripped": stripped, "root": root}
